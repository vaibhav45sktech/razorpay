"""Turn a Candidate's facts into one friendly sentence.

The model here is small (config.WATCHER_MODEL) and its job is narrow: rewrite
`template` in a warmer voice WITHOUT changing any number. It is
format-constrained to {"text": "..."} and its output is checked — every
rupee figure that appears in the template must still appear in the model's
text, and the text must be short. If the model is unavailable, slow, or
changes a number, the template is used and `phrased_by` says so. Facts are
passed as DATA; the only free text an outside party could have influenced
(an offer title) is quoted inside the template, and a suggestion has no
capability to act on anything, so an injected title can at worst read oddly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from backend import config
from backend.agent import llm_client
from backend.watcher.rules import Candidate

logger = logging.getLogger("campuspool.watcher.phrasing")

_FORMAT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}

_SYSTEM = (
    "You write ONE short, warm sentence (max 45 words) for a student savings app, in English, "
    "from the facts and the draft sentence you are given. Keep every number and currency amount "
    "exactly as written in the draft. Do not add advice, promises, or new numbers. Do not mention "
    "that you are an AI. Respond with JSON: {\"text\": \"...\"}"
)

_MONEY_RE = re.compile(r"₹[\d,]+(?:\.\d{1,2})?|\b\d+%")


def _numbers(text: str) -> list[str]:
    return sorted(set(_MONEY_RE.findall(text)))


def phrase(candidate: Candidate) -> tuple[str, str]:
    """Return (text, phrased_by)."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"Facts (data): {json.dumps(candidate.facts, ensure_ascii=False)}\n"
                f"Draft sentence: {candidate.template}\n"
                "Rewrite the draft sentence warmly, keeping every number."
            ),
        },
    ]
    try:
        obj: Any = llm_client.chat_json(messages, _FORMAT, temperature=0.3, model=config.WATCHER_MODEL)
        text = str(obj.get("text", "")).strip() if isinstance(obj, dict) else ""
    except (llm_client.LLMUnavailable, llm_client.LLMMalformedOutput) as exc:
        logger.info("watcher phrasing fell back to template (%s)", exc)
        return candidate.template, "template"

    if not text or len(text) > 400 or _numbers(text) != _numbers(candidate.template):
        logger.info("watcher phrasing rejected model text (changed numbers or length); using template")
        return candidate.template, "template"
    return text, f"llm:{config.WATCHER_MODEL}"
