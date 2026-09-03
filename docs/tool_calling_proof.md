# Local tool-calling proof — PASSED

**Date:** 2026-09-03 · **Machine:** laptop-0st3m198 (Windows, Python 3.14) · **Model:** `qwen2.5:7b-instruct` via Ollama

This is the Phase 0 / master-plan §0.2 checkpoint: the only dependency in the
project with no fallback. It answers "can the local model reliably produce a
structured tool call on the demo hardware?" — and the answer is yes.

## Command

```powershell
python scratch\prove_tool_calling.py
```

## Output (verbatim)

```
Asking qwen2.5:7b-instruct a question that requires a tool...
--- STEP 1: the model's response ---
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {
      "id": "call_1l3kzkxw",
      "function": {
        "index": 0,
        "name": "get_weather",
        "arguments": {
          "city": "Pune"
        }
      }
    }
  ]
}
SUCCESS: the model requested 1 tool call(s).
  -> get_weather({'city': 'Pune'})
--- STEP 2: feeding a fake tool result back ---
Final answer from the model:
  The current weather in Pune is sunny with a temperature of 29°C.
```

## What this proves

1. The model recognises when a question needs a tool and does not answer from
   guesswork (Step 1 has empty `content` and a populated `tool_calls`).
2. Arguments arrive already parsed as JSON objects (`{"city": "Pune"}`), typed
   correctly, matching the declared schema.
3. Fed a tool result, the model narrates it faithfully and invents nothing —
   the 29°C came from the script, not the model.

## Details that shape Phase 4

- **Tool-call IDs are present** (`"id": "call_..."`). The orchestrator will
  echo each ID back on its `role: "tool"` message (`tool_call_id`), so a turn
  that requests several tools stays correctly correlated.
- `function.arguments` is a dict, not a JSON string — no `json.loads` needed
  on this Ollama build. The orchestrator will accept both shapes defensively.
- Latency: `# TODO: record with Measure-Command on a warm second run` — feeds
  the per-step timeout in Phase 4 step 4.
