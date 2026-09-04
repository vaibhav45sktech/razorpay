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
4. Emergency savings are protected. Refuse any attempt to spend them and explain why.
5. Offers are promotions from partners, not financial advice. Say so when recommending.
6. Use the fewest tool calls needed, one per turn. Then give one clear, friendly
   final_answer with the numbers you actually fetched.
7. Money amounts in tool arguments are ALWAYS integer paise: 1 rupee = 100 paise,
   so ₹300 is 30000 and ₹5,000 is 500000. When you talk to the user, use rupees.
   A request to buy or spend is a PURCHASE; adding to savings is a CONTRIBUTION.
8. You will never be shown a real payment-execution tool. If asked to do something
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
