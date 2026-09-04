"""agent/prompts.py — the single system prompt, plus the tool catalog text
that stands in for Ollama's native `tools` field (HLD s2.7, adapted per the
Phase 4 Pre-Build Research Brief).

Because agent/llm_client.py deliberately never sends `tools` (see its module
docstring for why), the model has no other way to learn what tools exist,
what they do, or what arguments they take — that information has to live in
the prompt as text. render_tool_catalog() builds it from the SAME ToolDef
registry entries llm_visible_tools() returns, so the catalog the model reads
and the enum agent/llm_client.decide() is grammar-constrained to can never
drift apart: one is text rendered from the other's source list.

One prompt, one agent. Keep it short and rule-shaped — the real rules live in
code (policy_engine, orchestrator.execute_tool); this prompt only makes the
model a cooperative, well-informed caller of that code. Iterate only with
benchmark evidence (HLD Part 5.4), never on vibes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.tool_registry import ToolDef

SYSTEM_PROMPT = """You are the Financial Agent for CampusPool, a DEMO student savings app.
All money is Razorpay TEST MODE - no real money exists anywhere.

You help with three domains: savings goals, the community pool, and rule-bound spending/offers.

You do not see a normal chat interface to the outside world. Every turn, you must respond
with EXACTLY ONE JSON object shaped like:
    {"action": "call_tool" | "final_answer", "tool_name": <string or null>, "final_text": <string or null>}
Set action to "call_tool" and tool_name to one tool from the list below when you need more
information or need to propose a money action. Set action to "final_answer" and put your
reply to the user in final_text when you have everything you need. Never set both tool_name
and final_text at once; the one that doesn't apply must be null.

Hard rules:
1. NEVER state a balance, transaction, payment status, reward, offer, or pool number
   from memory. Always call a tool first. If a tool didn't return it, say you
   don't know and offer to check.
2. Before proposing any payment, contribution, or purchase, you MUST call check_policy.
   If it returns DENY, explain the reason and stop - even if the user insists.
   If REQUIRE_APPROVAL, tell the user approval is needed; do not proceed yourself.
3. Never claim a payment succeeded. After create_payment_intent, say it is pending
   confirmation. Success is announced only from verified ledger data on a later turn.
   Never say "let's proceed", "processing", "sending" or "done" unless you actually
   called create_payment_intent THIS turn and it returned a pending intent. Words
   are not actions: if you did not call the tool, nothing happened - say so.
4. Emergency savings are protected. Refuse any attempt to spend them and explain why.
5. Offers are promotions from partners, not financial advice. Say so when recommending.
6. Use the fewest tool calls needed, one per turn. Never call the same tool with the
   same arguments twice in one turn - the answer cannot change. Then give one clear,
   friendly final_answer with the numbers you actually fetched.
7. Money amounts in tool arguments are in RUPEES, exactly the number the user said:
   ₹5,000 is 5000, ₹300 is 300. Never convert, never round, never change it.
   Fields in the state or a tool result whose name ends in _paise are in paise:
   divide by 100 before quoting them (50000 paise is ₹500, NOT ₹50,000). Prefer the
   rupee figures already given in the state summary and in tool "reason" text.
   A request to buy or spend is a PURCHASE; adding to savings is a CONTRIBUTION.
8. If the user asks you to pay, send, spend or contribute but neither this message
   nor the earlier conversation says HOW MUCH and WHAT FOR, ask them - as a
   final_answer. Never guess an amount or invent a purpose. Never substitute a
   different amount or a different action for what the user asked: if ₹5,000 is
   denied, say so and stop. You may invite the user to name a smaller amount, but
   you must never choose one for them - the system blocks amounts the user did not type.
9. You will never be shown a real payment-execution tool. If asked to do something
   that sounds like directly moving money to a real card, a loan, or investment
   returns, decline and explain this is a demo scoped to savings, pooling and
   policy-bound purchases.
"""


def render_tool_catalog(tools: list[ToolDef]) -> str:
    """Render the LLM-visible tool registry as prompt text.

    Each entry shows the tool's name, description, and its JSON schema
    (the exact schema agent/llm_client.fill_arguments() will grammar-
    constrain the model's answer to once this tool is chosen) so the model
    knows what shape of arguments to plan for before it ever gets there.
    """
    if not tools:
        return "(no tools are currently available)"

    blocks = []
    for tool in tools:
        schema = tool.args_json_schema()
        blocks.append(
            f"- {tool.name}: {tool.description}\n"
            f"  arguments schema: {json.dumps(schema, separators=(',', ':'))}"
        )
    return "\n".join(blocks)


def render_fill_instruction(tool: ToolDef, user_message: str) -> str:
    """The step-2 task framing for agent/llm_client.fill_arguments().

    Step 2 is grammar-constrained to the tool's schema, which guarantees the
    SHAPE of the answer but says nothing about its MEANING. The 2026-09-04
    manual adversarial run showed what happens without this: handed only the
    conversation (whose last message is its own routing JSON) and a schema,
    qwen2.5:7b-instruct filled check_policy with the last enum value of
    `action` twice in a row (TEST_PAYOUT, then CONTRIBUTION) for a plain
    purchase request. So step 2 now restates the job explicitly: which tool,
    what the user actually asked, and each field's meaning, in one message.
    It is appended for the fill call only and never kept in the transcript.
    """
    schema = tool.args_json_schema()
    props: dict = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    if not props:
        return f"Call {tool.name} takes no arguments. Respond with an empty JSON object: {{}}"

    lines = []
    for field, spec in props.items():
        desc = spec.get("description", "")
        # Literal/enum fields render as anyOf/enum in Pydantic's JSON schema.
        enum = spec.get("enum")
        if enum is None and "anyOf" in spec:
            for alt in spec["anyOf"]:
                if "enum" in alt:
                    enum = alt["enum"]
                    break
        choice = f" One of: {', '.join(map(str, enum))}." if enum else ""
        req = "required" if field in required else "optional, may be null"
        lines.append(f"- {field} ({req}):{choice} {desc}".rstrip())

    return (
        f"You chose the tool {tool.name}. Now provide ONLY its arguments as one JSON object.\n"
        f"The user's request was: \"{user_message}\"\n"
        "Fill each field from that request:\n" + "\n".join(lines)
    )


def _rupees(paise: int | None) -> str:
    if paise is None:
        return "unknown"
    return f"₹{paise / 100:,.2f}"


def render_state_summary(state: dict) -> str:
    """A short, deterministic, rupee-denominated reading of the verified state
    that goes in FRONT of the raw JSON snapshot every turn.

    The raw snapshot is authoritative and stays (same numbers the UI shows);
    this summary exists because the 2026-09-04 real-model run showed
    qwen2.5:7b-instruct quoting `50000` paise as "₹50,000". Rendering the
    headline figures in rupees ourselves removes the conversion the model was
    getting wrong. Nothing here is computed from anything but `state`.
    """
    lines = []
    user = state.get("user") or {}
    if user.get("name"):
        lines.append(f"User: {user['name']}")

    balances = state.get("balances_paise") or {}
    if "emergency_savings" in balances:
        lines.append(f"Emergency savings (protected, never spendable): {_rupees(balances['emergency_savings'])}")
    if "rewards" in balances:
        lines.append(f"Rewards balance: {_rupees(balances['rewards'])}")

    spend = state.get("spending_this_month") or {}
    if spend:
        lines.append(
            "Discretionary spending this month: "
            f"{_rupees(spend.get('used_paise'))} used of {_rupees(spend.get('limit_paise'))} limit, "
            f"{_rupees(spend.get('remaining_paise'))} remaining"
        )

    policy = state.get("policy") or {}
    if policy:
        lines.append(
            f"Any single purchase above {_rupees(policy.get('approval_threshold_paise'))} needs the user's "
            "explicit approval in the app (not from a parent or anyone else)"
        )
        if policy.get("per_tx_limit_paise"):
            lines.append(f"Per-transaction limit: {_rupees(policy['per_tx_limit_paise'])}")

    goals = state.get("goals") or []
    for g in goals[:3]:
        lines.append(
            f"Goal '{g.get('label')}': {_rupees(g.get('current_paise'))} of {_rupees(g.get('target_paise'))} "
            f"({g.get('pct_complete')}% complete)"
        )

    pending = state.get("pending_actions") or []
    if pending:
        lines.append(f"{len(pending)} pending money action(s) awaiting the user's decision in the app")
    else:
        lines.append("No pending money actions")

    return "\n".join(lines)
