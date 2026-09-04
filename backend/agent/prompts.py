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
7. You will never be shown a real payment-execution tool. If asked to do something
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
