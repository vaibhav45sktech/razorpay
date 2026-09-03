"""Phase 0, Step 10 — prove the core agent mechanism works on YOUR machine.

RUN THIS ON YOUR WINDOWS MACHINE (where Ollama is installed), not anywhere else.
Ollama must be running and the model pulled:

    ollama pull qwen2.5:7b-instruct
    python scratch/prove_tool_calling.py

This is throwaway scaffolding, deliberately NOT part of the app. Its only job is
to answer one question before you invest days in the project:

    "Can the local model reliably produce a structured tool call?"

If this script prints a tool_calls block, the single dependency with no fallback
is confirmed working. If it does not, stop and fix that before Phase 1 — every
later phase compounds on it.

See Building_Your_First_AI_Agent.md, Chapter 2, for what this is demonstrating.
"""

import json

import httpx

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"

# A deliberately trivial tool. Note the DESCRIPTION quality — small local models
# lean heavily on this text to decide whether a tool is relevant. Vague
# descriptions are the #1 reason a model never calls your tool.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get the current weather for a given city, including "
                "temperature in Celsius and sky conditions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name, e.g. 'Pune'"}
                },
                "required": ["city"],
            },
        },
    }
]


def main() -> None:
    print(f"Asking {MODEL} a question that requires a tool...\n")

    messages = [{"role": "user", "content": "What's the weather like in Pune right now?"}]

    try:
        response = httpx.post(
            OLLAMA_URL,
            json={"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False},
            timeout=180,
        )
        response.raise_for_status()
    except httpx.ConnectError:
        print("FAILED: could not reach Ollama at http://localhost:11434")
        print("Is Ollama running? Try: ollama run qwen2.5:7b-instruct \"hi\"")
        return
    except httpx.HTTPStatusError as exc:
        print(f"FAILED: Ollama returned HTTP {exc.response.status_code}")
        print(exc.response.text[:500])
        return

    message = response.json()["message"]
    print("--- STEP 1: the model's response ---")
    print(json.dumps(message, indent=2))

    tool_calls = message.get("tool_calls")
    if not tool_calls:
        print("\nRESULT: no tool_calls in the response.")
        print("The model answered in plain text instead of requesting the tool.")
        print("This usually means weak tool-calling support in the model, or a")
        print("description the model didn't find compelling. See Chapter 9 of the book.")
        return

    print(f"\nSUCCESS: the model requested {len(tool_calls)} tool call(s).")
    for call in tool_calls:
        fn = call["function"]
        print(f"  -> {fn['name']}({fn['arguments']})")

    # STEP 2: feed a FAKE result back and watch it narrate an answer. Notice that
    # WE invent the 29C — the model just faithfully reports what the tool said.
    # That is the whole architecture in miniature.
    print("\n--- STEP 2: feeding a fake tool result back ---")
    messages.append(message)
    messages.append(
        {
            "role": "tool",
            "name": "get_weather",
            "content": json.dumps({"city": "Pune", "temp_c": 29, "condition": "sunny"}),
        }
    )

    follow_up = httpx.post(
        OLLAMA_URL,
        json={"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False},
        timeout=180,
    )
    follow_up.raise_for_status()
    print("Final answer from the model:")
    print(f"  {follow_up.json()['message']['content'].strip()}")
    print("\nThe 29C came from THIS script, not the model. That gap between")
    print("'model narrates' and 'code supplies the facts' is the entire design.")


if __name__ == "__main__":
    main()
