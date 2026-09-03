# Building Your First AI Agent — A Beginner's Understanding Book

**A companion to CampusPool_Agent_HLD_LLD.md and CampusPool_Build_Plan.md**
**Who this is for:** you've never built an AI agent before, and you want to actually *understand* what you're building — not just copy-paste code that works by accident.

**How to use this book:** read it in order. Each chapter has (1) the idea explained in plain English with an analogy, (2) a small runnable code example you should actually type and run — not just read, (3) a "try this yourself" exercise, and (4) a "beginner trap" — a mistake almost everyone makes at this exact step. By the end, you won't just have copied the CampusPool design — you'll understand *why* every piece of it exists, which means you can debug it when it breaks (and it will break).

Total reading + hands-on time: roughly 6–10 hours spread over a couple of days, before you touch the real project code.

---

## Table of Contents

- Chapter 0 — Reset your mental model of "AI agent"
- Chapter 1 — How the LLM underneath actually behaves (just enough to build with)
- Chapter 2 — Tool calling: the one trick that makes agents possible
- Chapter 3 — Build the smallest possible agent, by hand, today
- Chapter 4 — The loop: multi-step reasoning and why it needs brakes
- Chapter 5 — Memory: what the agent "remembers" and what it doesn't
- Chapter 6 — The dangerous part: letting an agent do real things safely
- Chapter 7 — How a real agent codebase is organized
- Chapter 8 — Testing an agent (without losing your mind)
- Chapter 9 — When it misbehaves: a field guide to failure modes
- Chapter 10 — Mapping everything you learned onto CampusPool
- Chapter 11 — Glossary + where to go next

---

## Chapter 0 — Reset your mental model of "AI agent"

If you've only seen agents from the outside — a chatbot that "does things" — you probably picture something like a little autonomous robot brain making decisions on its own. Let go of that picture. It's wrong in a way that will actively confuse you while building.

**Here's the honest picture:** an AI agent is an ordinary computer program with a `while` loop in it. Inside that loop, one of the steps happens to call a language model. That's it. There is no robot brain. There is a loop, and a text-prediction model that's very good at reading instructions and writing structured responses.

An analogy: think of the LLM as a **very well-read intern who cannot leave their desk**. They can read anything you hand them and write incredibly good answers or instructions. But they have no hands. If you want something done — check a balance, send a message, look something up — the intern has to write you a note saying "please check the balance for user X and tell me the result," and *you* (the program) are the one who actually walks over, checks it, and brings the answer back. The intern never touches anything directly. They only ever read and write text.

That's the entire trick behind every AI agent that exists today, including expensive commercial ones. Once this clicks, agent-building stops being mysterious and becomes: "what notes can the intern write, and what do I do when I receive each kind of note?"

**Beginner trap:** thinking you need a special "agent framework" (LangChain, CrewAI, AutoGPT, etc.) to build this. You don't. Those frameworks are just pre-built versions of the loop you're about to write yourself in about 40 lines of code. Building it yourself first means you'll actually understand what those frameworks are doing when you eventually meet them — and for CampusPool, your project rules explicitly want you to build it yourself anyway.

---

## Chapter 1 — How the LLM underneath actually behaves (just enough to build with)

You don't need a machine learning degree to build an agent. You need about six facts, held firmly:

**1. An LLM is a text-in, text-out function.** You give it a chunk of text (called the *prompt* or, in a chat setting, a list of *messages*), and it produces more text, one small chunk at a time, based on "what usually comes next" given everything it's seen before, including its training data.

**2. It has no memory between calls.** Every single time you call the model, it only knows what's in the text you sent *this time*. If you want it to "remember" the last thing the user said, you have to literally paste the previous conversation back into the next call. This surprises almost every beginner. There is no session, no persistent brain — just you resending the whole conversation, every single turn.

**3. It works in "tokens," not words.** A token is roughly three-quarters of a word. "Contribution" might be 2–3 tokens. This matters for one practical reason: every model has a maximum number of tokens it can read+write in one call (the *context window*). Send too much conversation history and it either errors out or starts "forgetting" the earliest parts.

**4. `temperature` controls randomness.** Near 0 = "give me the most likely, most boring, most consistent answer." Near 1+ = "get creative, take risks." For an agent making decisions about money, you always want it near 0. You are not writing poetry; you want the same input to reliably produce the same kind of output.

**5. It can lie confidently — this is called "hallucination."** If you ask it "what's my account balance?" with no way to look it up, it will often just... make up a plausible-sounding number. Not because it's malicious — because its entire job is "produce plausible-sounding text," and a plausible-sounding number *is* plausible-sounding text. This single fact is the entire reason the rest of this book exists: the fix isn't "ask it not to lie," the fix is "never let it be the source of a number that matters."

**6. It follows instructions in the prompt, but instructions are requests, not laws.** If your prompt says "always check the database before answering," a good model will usually comply — but "usually" is not "always," especially with smaller/local models. This is why, later, your *code* — not your prompt — will be what actually enforces the rules that matter.

**Try this yourself:** open a chat with any LLM (Claude, ChatGPT, or your local Ollama model — doesn't matter which for this exercise) and ask it something you're confident it can't know, like "what's the current balance of my bank account?" Watch what it does. Does it say "I don't have access to that," or does it invent a number, or ask a clarifying question? This single test tells you a lot about how much you can trust a given model's honesty under uncertainty — and it's exactly the failure mode Chapter 6 exists to prevent structurally.

**Beginner trap:** assuming a bigger/smarter model "solves" hallucination. It reduces it, but never to zero, and a local 7B model will hallucinate more readily than a huge hosted one. This is precisely why CampusPool's design never trusts the model's own claims about money — more on that soon.

---

## Chapter 2 — Tool calling: the one trick that makes agents possible

Here's the actual mechanism behind "the intern writes you a note asking you to check something."

Normally, when you send an LLM a message, it just writes back a text reply. **Tool calling** (also called *function calling*) is a special mode where you also send along a list of "things I know how to do" — described as a small JSON schema — and the model is allowed to respond with either (a) a normal text reply, or (b) a structured request like:

```json
{
  "tool_calls": [
    {
      "function": {
        "name": "get_weather",
        "arguments": { "city": "Pune" }
      }
    }
  ]
}
```

That's it. That's the entire mechanism. The model isn't *executing* `get_weather` — it's **writing down, in a precise machine-readable format, that it would like you to run something called `get_weather` with `city="Pune"`**. Your code reads that JSON, notices it matches a real function you wrote, actually runs that function, and sends the *real result* back to the model as another message. The model then uses that real result to write its final answer.

Why does this work reliably? Because modern models (including small local ones like Qwen) are specifically trained on huge amounts of exactly this pattern — "here's a schema, here's a request, produce matching JSON" — so they're very good at it, much better than at, say, doing arithmetic reliably in free text.

**The schema is the contract.** Every tool you define needs:
- a **name** ("get_weather")
- a **description** in plain English (this is what the model reads to decide *when* to use it — vague descriptions are the #1 cause of a model never calling your tool, or calling the wrong one)
- an **input schema** (what arguments it needs, and their types)
- (in your own code, not sent to the model) an **output schema** and a **handler function** that actually does the work

**Try this yourself — the single most important exercise in this book.** Run this script (needs Ollama running locally with a tool-calling-capable model pulled, e.g. `ollama pull qwen2.5:7b-instruct`):

```python
import httpx, json

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a given city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}]

response = httpx.post("http://localhost:11434/api/chat", json={
    "model": "qwen2.5:7b-instruct",
    "stream": False,
    "messages": [{"role": "user", "content": "What's the weather like in Pune right now?"}],
    "tools": tools,
}, timeout=120)

print(json.dumps(response.json()["message"], indent=2))
```

Run it. You should see a `tool_calls` field in the output, with `"name": "get_weather"` and `"arguments": {"city": "Pune"}`. **Stop and actually look at this output before continuing.** You just watched the entire mechanism happen. The model never called any weather API — it doesn't have one — it just correctly recognized "this question needs a tool I was told about" and wrote the matching JSON request. Everything else in this book is elaboration on this one moment.

Now feed it a fake result and watch it compose a real answer:

```python
messages = [
    {"role": "user", "content": "What's the weather like in Pune right now?"},
    response.json()["message"],  # the assistant's tool_calls message
    {"role": "tool", "name": "get_weather", "content": json.dumps({"city": "Pune", "temp_c": 29, "condition": "sunny"})},
]
response2 = httpx.post("http://localhost:11434/api/chat", json={
    "model": "qwen2.5:7b-instruct", "stream": False, "messages": messages, "tools": tools,
}, timeout=120)
print(response2.json()["message"]["content"])
```

You'll get back something like "It's currently sunny in Pune at 29°C." Notice: **you** made up the 29°C. The model just faithfully reported it. This is the whole game — the model is a translator between "vague human intent" and "precise tool calls," and a narrator that turns "precise tool results" back into "friendly human language." It should never be the one inventing the 29°C for real.

**Beginner trap:** writing a description like `"Gets weather"` instead of `"Get the current weather for a given city, including temperature and conditions"`. Small/local models lean heavily on the description text to decide when a tool is relevant — vague descriptions cause the model to either never call the tool, or call the wrong one when you have several similar tools. Write descriptions like you're explaining it to a new coworker, not like a code comment.

---

## Chapter 3 — Build the smallest possible agent, by hand, today

You now know both ingredients: an LLM that can request tool calls, and the fact that *you* run the real function. An "agent" is just: keep doing that back-and-forth in a loop until the model stops asking for tools and gives you a final answer.

Here is a complete, tiny, real agent — no framework, about 30 lines of actual logic:

```python
import httpx, json

def get_weather(city: str) -> dict:
    # pretend "real" function — in your real project this would query a database
    fake_data = {"Pune": 29, "Mumbai": 32, "Delhi": 38}
    return {"city": city, "temp_c": fake_data.get(city, 25), "condition": "sunny"}

TOOLS_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather (temperature in Celsius and condition) for a given city",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}]
TOOL_FUNCTIONS = {"get_weather": get_weather}

def run_agent(user_message: str, max_steps: int = 5) -> str:
    messages = [{"role": "user", "content": user_message}]

    for step in range(max_steps):
        r = httpx.post("http://localhost:11434/api/chat", json={
            "model": "qwen2.5:7b-instruct", "stream": False,
            "messages": messages, "tools": TOOLS_SCHEMA,
        }, timeout=120)
        msg = r.json()["message"]
        messages.append(msg)

        if not msg.get("tool_calls"):
            return msg["content"]                      # model gave a final answer — done

        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if name not in TOOL_FUNCTIONS:
                result = {"error": f"unknown tool {name}"}
            else:
                result = TOOL_FUNCTIONS[name](**args)   # <-- YOUR code actually runs, not the model
            messages.append({"role": "tool", "name": name, "content": json.dumps(result)})

    return "I couldn't finish within my step budget."

print(run_agent("Should I carry an umbrella in Mumbai today?"))
```

Run this. Watch it: call `get_weather("Mumbai")`, get back `32°C, sunny`, and then reason "no need for an umbrella" in its final text answer — all without you writing any umbrella-reasoning logic yourself. That reasoning came from the model reading the tool result and using its language understanding. That's genuinely what "agentic" means: not magic, just *the model deciding which tools to use and how to interpret results, inside a loop your code controls.*

**Try this yourself:** add a second tool, `get_traffic(city)`, returning a fake congestion level, and ask "Should I leave now for Mumbai and will the weather or traffic be a problem?" Watch the model call *both* tools before answering — you didn't tell it to call both, it figured that out from your question and the tool descriptions. This is "planning," demystified.

**Beginner trap:** forgetting to append the model's own `tool_calls` message (`messages.append(msg)`) before appending the tool's result. If you skip this, the model loses track of *which* tool call the result belongs to, and either errors out or starts hallucinating. The conversation history must contain, in order: your question → the model's tool request → the tool's result → (repeat) → the model's final answer. Every agent bug in your first week will trace back to a broken message history — get comfortable printing `messages` and reading it line by line when something goes wrong.

---

## Chapter 4 — The loop: multi-step reasoning and why it needs brakes

Look again at `run_agent` above — specifically `max_steps`. Why does that exist?

Because nothing *guarantees* the model eventually stops asking for tools. A confused model, a bad tool description, or a tool that keeps returning something the model doesn't understand can all cause the loop to keep calling tools forever, burning time and (if you're paying per-call) money. Real agents cap the number of steps and give up gracefully — reporting honestly that they couldn't finish, rather than either hanging forever or (worse) inventing a fake final answer to escape the loop.

This "observe → decide → act → observe result → repeat" pattern has a name in the AI literature: **ReAct** (Reasoning + Acting). You don't need to memorize the term, but you should recognize the shape, because you'll see it everywhere: the model alternates between "thinking about what to do" and "doing a thing and looking at the result," rather than trying to plan the entire task in one shot. This is *why* agents can handle open-ended requests that a single prompt-response can't: the model gets to react to real information as it arrives, instead of guessing everything up front.

There's a second, quieter reason step budgets matter: **cost and latency control.** Every step is a full round trip to the model (even a local one takes a second or more). A user asking a simple question shouldn't wait 30 seconds because the model got indecisive. In the CampusPool design, `MAX_STEPS = 8` — generous enough for a real multi-tool task, tight enough to fail fast and visibly instead of hanging.

**What "planning" really looks like in your loop:** the model isn't executing a separate "planning phase" — it's just that each time through the loop, it looks at *everything so far* (original question + every tool result received) and decides the single next best action. Multi-step plans emerge from this one-step-at-a-time decision process, the same way a person solving a puzzle doesn't plan every move in advance — they react to the board as it changes.

**Try this yourself:** in your `run_agent` from Chapter 3, add a `print(f"step {step}: model wants {[c['function']['name'] for c in msg.get('tool_calls', [])]}")` line inside the loop. Ask a question needing 2–3 tools and watch the plan unfold turn by turn in your terminal. This single debugging habit — printing what the model asked for, every step — will save you more time than anything else in this book once you're debugging a real, larger agent.

**Beginner trap:** setting `max_steps` too low (like 1 or 2) "to save money," and then being confused when the agent gives weird half-answers. A question needing two tool calls plus a final answer needs *at least* 3 steps in this loop design. Start generous (8–10), watch real usage, then tune down if needed — don't guess low up front.

---

## Chapter 5 — Memory: what the agent "remembers" and what it doesn't

Remember fact #2 from Chapter 1: the model has no memory of its own. Every "memory" your agent appears to have is really just **you re-sending the conversation history** as part of the `messages` list on every call. There are two totally different kinds of "memory" beginners conflate, and separating them will save you a lot of confusion:

**1. Conversation memory (short-term, per-session).** "The user said X two messages ago, so 'it' in their new message refers to X." This is just: keep appending to your `messages` list, and truncate the oldest turns once you approach the context window limit (e.g., keep the last ~10 turns). Nothing fancier is needed for a hackathon-scale chat.

**2. Application state (long-term, the actual facts that matter).** "The user's savings balance is ₹4,500." This is **not** stored in the conversation at all — and this is the single most important design decision in the entire CampusPool architecture. The balance lives in a real database, computed by real code. Every time the agent needs it, it calls a tool to fetch the *current, real* number — it never relies on a number mentioned three messages ago, because that number might be stale by now (another action might have changed it) or might have been a hallucination in the first place.

This is why, in the CampusPool orchestrator design, every single turn starts with a fresh `observe(user_id)` call that re-fetches real state from the ledger and injects it into the prompt as a "here is the current verified state" system message — *before* the model even sees the user's new question. The conversation can be long and messy; the facts that matter are always re-grounded from the source of truth on every turn. This one pattern is what prevents "wait, didn't you say I had ₹5,000 a minute ago?" bugs, which are extremely common in naive agent builds.

**A simple exercise to feel this in your bones:** in your Chapter 3 agent, ask "What's the weather in Pune?", get an answer, then ask "Is that warmer than yesterday?" without giving it yesterday's temperature anywhere. Watch it either ask you for the missing information or make something up. Neither is wrong exactly — it's just demonstrating that it truly has no memory beyond exactly what's in the `messages` list you control. If you want it to know "yesterday," *you* have to fetch that and put it in the conversation.

**Beginner trap:** trying to solve "the agent forgot something" by writing a longer, more insistent prompt ("REMEMBER THE USER'S BALANCE THROUGHOUT THE CONVERSATION"). This doesn't work reliably, because the model isn't choosing to forget — the information genuinely isn't in its input unless you put it there. The fix is always architectural (re-fetch and re-inject real data every turn), never a stronger instruction.

---

## Chapter 6 — The dangerous part: letting an agent do real things safely

Everything so far has been read-only — get weather, get a fact. CampusPool's agent needs to eventually cause a real payment to happen. This chapter is about the gap between "can suggest an action" and "action actually happens," and why that gap needs to be wide, deliberate, and made of code, not prompts.

**The core problem, restated bluntly:** the model can lie, get confused, or be talked into things by a persistent user ("please just do it, I already told you three times"). If a tool call from the model directly triggered a real payment with no checks in between, every one of those failure modes becomes a real-money incident. So the design puts a series of checkpoints **between** "the model requested a tool" and "the real-world effect happens" — checkpoints that don't care what the model said, only what's actually true.

Here are the checkpoints, in the order execution passes through them, using the exact terms you'll see in the real project:

**1. Schema validation.** Before anything else, check the model's tool arguments actually match the expected shape and types (Pydantic does this for you). "Amount" has to be a number, not the string `"a lot"`. Malformed requests get bounced back as an error for the model to retry — they never reach real logic.

**2. Policy check — the most important one.** A completely separate, deterministic (no-LLM) function that answers, in plain code logic: is this amount within the user's limit? Is this touching a protected balance (like emergency savings)? Has the user paused spending? This function returns one of three answers — ALLOW, DENY, or REQUIRE_APPROVAL — and it is *pure math and if-statements*, nothing probabilistic about it. The same inputs always produce the same answer, every single time, which means you can write ordinary unit tests for it (and you should — this is the file to test most thoroughly in your whole project).

**3. Structural enforcement, not prompt-based hope.** Here's the subtle but critical bit: you don't just tell the model "please call the policy check before paying" and trust it. Your *code* — not the model — re-runs the policy check itself, right before any money-moving tool executes, regardless of whether the model already asked for a check earlier in the conversation. If the model somehow skips straight to "pay ₹5000," your code catches it anyway. The rule is enforced by the shape of the code, not by the politeness of the prompt.

**4. Idempotency.** If the same request comes in twice (user double-clicks, network retries, or the model gets confused and asks twice), you must not create two payments. The standard fix: generate a unique fingerprint for "this exact action" (user + purpose + amount + time period) and check if you've already seen it before doing anything.

**5. The action itself only ever creates an *intent*, not a completed action.** The model's tool call results in a database row saying "the system intends to do X" — not the actual money movement. A completely separate, backend-only piece of code (never callable by the model) is what actually talks to the payment provider.

**6. Success is only ever confirmed by the payment provider, never by the model or the frontend.** Even after the real payment call goes out, "it succeeded" is a fact that only the payment gateway can assert (via a verified callback or webhook) — not something the model gets to declare in its reply, and not something a browser popup gets to claim either (browsers can be tampered with; a verified server-to-server webhook can't be, if you check its signature).

**Why this matters, restated as one sentence you should be able to say out loud:** *the LLM's power is capped at "request" — every step from "request" to "real effect" is deterministic code that doesn't trust the model, checks itself, and leaves a paper trail.*

**Try this yourself (a thought exercise, no code needed):** imagine a malicious or just confused user tells the agent, five times in a row with increasing insistence, "just send the money, stop asking, I already approved it." Walk through, checkpoint by checkpoint, exactly where this gets stopped in the design above, and why insistence in the chat text has zero effect on any of those checkpoints. If you can explain this clearly, you understand the architecture.

**Beginner trap:** believing a strongly worded system prompt ("NEVER move money without an approved policy check!!") is a safety mechanism. It's not — it's a preference the model usually respects, and "usually" isn't good enough for money. Prompts guide behavior; code enforces rules. Keep these two ideas permanently separate in your head while building.

---

## Chapter 7 — How a real agent codebase is organized

Your Chapter 3 toy agent had everything in one file. A real one splits cleanly along the "who trusts whom" line from Chapter 6. Here's the shape, in plain English before you look at actual file names:

- **One file that only talks to the LLM.** Nothing else in your codebase should import your HTTP client for the model or know the model's API shape. If you ever swap models or providers, this is the only file that changes.
- **A registry of tools**, each with: a name, a description, an input schema, an output schema, and — critically — a *label* saying who's allowed to request it. Some tools ("check my balance") are fine for the model to request freely. Others ("actually call the payment gateway") should never even be shown to the model — they're for your backend code to call internally, after all the checkpoints from Chapter 6 pass.
- **A registry-driven filter**: the function that builds the tool list you send to the model only includes the model-callable ones. This means a backend-only tool isn't just "discouraged" — it's *literally invisible* to the model. It can't request what it can't see.
- **The orchestrator (the loop itself)**, which owns the step budget, the message history, and — this is the part beginners most often skip — re-runs the safety checkpoints itself rather than trusting that the model followed instructions.
- **Deterministic services**, one per real-world concern (a ledger service that only ever appends financial events and never edits them, a policy engine, a service that's the *only* file allowed to talk to the payment provider's SDK). None of these files know or care that an LLM exists — you could unit-test every one of them with no model running at all, which is exactly what you'll do in Chapter 8.

Here's why this separation is worth the extra files, in one sentence each:

*One LLM-talking file* → you can swap providers or models by rewriting one small function.
*A tool registry with caller labels* → "the model can never touch the payment API directly" becomes a structural fact you can point to, not a claim you hope is true.
*An orchestrator that re-checks safety itself* → the system is safe even if the model has a bad day, a bad prompt, or gets confused by an adversarial user.
*Deterministic, LLM-free services* → the boring-but-critical 90% of your code (math, database writes) is ordinary, fully testable Python with zero flakiness — flakiness gets contained entirely inside the one small area where the model is actually involved.

If you go back and skim `CampusPool_Agent_HLD_LLD.md` §2.1 (module map) and §2.3 (tool contract) after reading this chapter, it should now read as "obviously reasonable" rather than "a lot of files for a hackathon." That's the goal of this book — by the time you open the real design doc's code, you should be nodding, not decoding.

**Try this yourself:** take your Chapter 3 single-file agent and split it into three files — `llm_client.py` (just the httpx call), `tools.py` (the `get_weather` function and its schema), `orchestrator.py` (the loop). Import across files correctly and confirm it still runs identically. This tiny refactor is the entire structural idea behind the real project, just at 1/50th the size.

**Beginner trap:** over-engineering this on day one — building five layers of abstraction before you've even gotten one tool call working end to end. Build the single-file version first (Chapter 3), get it working, *then* split it once you understand why each piece exists. Structure that you understand because you needed it beats structure copied from a diagram.

---

## Chapter 8 — Testing an agent (without losing your mind)

New agent-builders often assume "testing an AI thing" means running it fifty times and eyeballing the replies. That's slow, inconsistent (the model might answer differently each time), and doesn't scale. Real agent testing happens in layers, from fastest/cheapest to slowest/most realistic — test at the lowest layer that can catch the bug.

**Layer 1 — test your plain functions like normal code, with zero LLM involved.** Your policy engine, your ledger math, your tool handlers — these are ordinary Python functions. Test them with ordinary `pytest`, the same way you'd test any function. This layer catches most bugs, runs in milliseconds, and never involves the model at all.

**Layer 2 — test the *loop's guarantees* using a fake, scripted model.** This is the trick that surprises most beginners the first time they see it, and it's genuinely clever: since your `llm_client.chat()` is just one function, you can swap it out in tests for a fake version that returns a pre-written script of responses instead of calling a real model. This lets you test things like "if the model tries to call a payment tool without a policy check, does my code block it anyway?" — deterministically, instantly, without ever touching Ollama:

```python
class ScriptedLLM:
    def __init__(self, script):
        self.script = iter(script)
    def chat(self, messages, tools):
        return next(self.script)   # just returns the next pre-written "model response"

def test_dangerous_tool_is_blocked_without_policy_check():
    fake = ScriptedLLM([
        {"tool_calls": [{"function": {"name": "make_payment", "arguments": {"amount": 5000}}}]},
        {"content": "done"},
    ])
    # run your real orchestrator with fake.chat swapped in for the real one
    # assert that no payment actually executed, because the structural policy
    # re-check (Chapter 6, checkpoint 3) caught it — even though the "model"
    # never called check_policy itself
```

This is how you prove your safety guarantees hold *no matter what the model does* — including a model that's buggy, adversarial, or just from a different, less well-behaved future version. You're not testing "is the model nice," you're testing "does my code protect against a model that isn't."

**Layer 3 — a benchmark suite against the real model.** Once the structural guarantees are proven in Layer 2, run a batch of realistic scenarios (tens to hundreds) against your actual local model, and check the *database* afterward for the right outcome — never grade based on the chat text looking roughly right, because "sounds plausible" is exactly the failure mode from Chapter 1. This is slower (each scenario is a real model call) but tells you how the real system behaves end to end, and gives you a number you can watch over time ("94% of scenarios behaved correctly") instead of a vague feeling.

**Layer 4 — you, personally, being adversarial.** Sit down and try to break it like a curious troublemaker: ask it to ignore its rules, ask the same thing five times with more urgency each time, ask about something entirely out of scope. This is slow and manual, so do it last, after the automated layers already give you confidence in the boring cases — save your human time for the genuinely tricky, creative attempts a script wouldn't think of.

**Try this yourself:** take the `run_agent` function from Chapter 3, and write one `ScriptedLLM`-based test that proves: "if the fake model asks for a tool name that doesn't exist in `TOOL_FUNCTIONS`, the loop doesn't crash — it returns a graceful error to the model and lets the conversation continue." This is a two-minute exercise that teaches you the whole Layer 2 pattern.

**Beginner trap:** only ever testing by chatting with the agent manually and "seeing if it feels right." This doesn't scale past a handful of cases, can't be run automatically before every change, and — because LLM output is genuinely a little random — the exact same test might pass one time and fail the next for no code-related reason. Push as much testing as possible down into Layers 1 and 2, where results are 100% deterministic.

---

## Chapter 9 — When it misbehaves: a field guide to failure modes

Every beginner hits some version of these. Recognizing which one you're looking at cuts debugging time from hours to minutes.

**"The model just answers in text instead of calling my tool."** Almost always a description problem (too vague — go back to the Chapter 2 trap), or the question genuinely doesn't need the tool (the model isn't wrong to skip it if it can answer without new information — ask yourself honestly whether a human would need to look something up here too), or the specific model/quantization you're running has weak tool-calling support (some small local models are much worse at this than others — this is a real hardware/model tradeoff, not something you can always prompt your way out of).

**"The model calls the right tool but with wrong or missing arguments."** Check that your schema's `required` fields and types are precise, and that your description explains *what the argument means*, not just its name. "amount" is worse than "amount in Indian Rupees, as a whole number, e.g. 500 for ₹500" — the more precisely you describe the expected shape, the fewer garbage arguments you'll get back.

**"The model calls a tool, gets a result, then calls the exact same tool again — and again."** Usually means the tool's *result* isn't being formatted clearly enough for the model to recognize "I now have what I need." Print the exact JSON you're sending back as the tool result and read it as if you were the model — is it obvious the question is answered? Sometimes it also means your `messages.append(msg)` ordering bug from Chapter 3 is confusing the model about what it already asked.

**"It hit the step budget and gave up."** Not actually a bug — it's the safety net from Chapter 4 doing its job. But if it happens often, something upstream is making the model indecisive: usually too many similar-sounding tools, vague descriptions that make several tools look equally relevant, or a genuinely too-hard multi-part question for a small local model to plan through in one pass.

**"It gave a confident, specific, completely wrong number."** This is hallucination (Chapter 1, fact #5), and if it's happening for something that *should* have come from a tool, it means either the tool wasn't called at all, or — more subtly — the model is ignoring the real tool result and reverting to its own guess in the final answer. The fix is never "tell it to stop lying" — it's checking whether the tool was actually invoked (print your message history), and if it was, tightening the system prompt's instruction to always use tool results verbatim rather than paraphrasing numbers from memory.

**"It worked five times in a row and then failed on the sixth, identical request."** Welcome to working with probabilistic systems. This is expected, not a sign you did something wrong. It's exactly why Chapter 6's structural checkpoints exist (so a bad response can't cause real harm) and why Chapter 8's Layer 3 benchmark measures a *percentage*, not a pass/fail — you're aiming for "reliable enough, with safety nets," not "never wrong."

**"It's slow."** Local models on a laptop are genuinely much slower than hosted APIs — this is the real cost of "no external API calls" from your earlier requirement. Mitigations: use a smaller/quantized model, reduce `max_steps`, keep tool descriptions and system prompts concise (shorter input = faster response), and — if the demo machine truly can't keep up — remember the `llm_client.py` isolation means falling back to a hosted API is a one-file change, not a redesign.

**A debugging habit worth building now:** whenever anything seems off, print the *entire* `messages` list right before the failing call, formatted with `json.dumps(messages, indent=2)`, and actually read it top to bottom like a transcript. The overwhelming majority of "why did it do that?" questions are answered by literally reading what the model actually saw — not what you assumed you sent it.

---

## Chapter 10 — Mapping everything you learned onto CampusPool

You now have every concept needed to read the real project design and understand *why*, not just *what*. Here's the direct mapping, so you can cross-reference as you build:

| What you learned here | Where it lives in the real project |
|---|---|
| Ch 2: tool = name + description + input/output schema | LLD §2.3, the full tool contract table |
| Ch 2/3: model requests, your code executes | `orchestrator.py`'s `execute_tool()` |
| Ch 4: step budget stops runaway loops | `MAX_STEPS = 8` in the orchestrator |
| Ch 5: re-fetch real state every turn, never trust chat history for facts | the `observe(user_id)` call at the top of every agent turn |
| Ch 6, checkpoint 1: schema validation | Pydantic input models on every tool |
| Ch 6, checkpoint 2: deterministic policy decision | `policy_engine.py` — pure functions, ALLOW/DENY/REQUIRE_APPROVAL |
| Ch 6, checkpoint 3: code re-checks itself, doesn't trust the model | the forced `policy_engine.check_policy()` call inside `execute_tool()`, regardless of what the model already did |
| Ch 6, checkpoint 4: idempotency | the `client_ref` hash on every `ActionIntent` |
| Ch 6, checkpoint 5: intent vs. real action | `create_payment_intent` (LLM-callable) vs. `create_razorpay_payment` (backend-only, invisible to the model) |
| Ch 6, checkpoint 6: success confirmed by the provider, not the model | Razorpay webhook + signature verification, never the chat reply |
| Ch 7: file organization by trust boundary | LLD §2.1 module map |
| Ch 8, Layer 1: plain function tests | pytest suites for `ledger_service`, `policy_engine` |
| Ch 8, Layer 2: scripted fake LLM | `ScriptedLLM` tests in LLD §5.3 |
| Ch 8, Layer 3: real-model benchmark | the 100-scenario benchmark in LLD §5.4 |
| Ch 8, Layer 4: adversarial manual testing | LLD §5.5 |
| Ch 9: debugging by reading the message transcript | your best friend during Phase 4 of the build plan |

Read `CampusPool_Agent_HLD_LLD.md` again after finishing this book — specifically Part 2 (the LLD) — and you should find yourself recognizing every decision as "yes, that's the checkpoint from Chapter 6" or "that's the memory pattern from Chapter 5," rather than encountering unfamiliar architecture. That recognition is the actual goal of this book — not memorizing the CampusPool design, but being able to *derive* it yourself from first principles, and therefore being able to adapt it confidently when reality doesn't match the plan (it won't, and that's fine).

---

## Chapter 11 — Glossary + where to go next

**Agent** — ordinary code with a loop, where one step in the loop calls an LLM and may act on its structured response.

**Tool / function calling** — a mode where the model can respond with a structured request to run a named function with specific arguments, instead of (or in addition to) plain text.

**Tool schema** — the JSON description of a tool's name, purpose, and expected arguments, sent to the model so it knows what's available.

**Orchestrator** — the code that owns the loop: sends messages + tools to the model, receives the response, executes any requested tools, and decides when to stop.

**Context window** — the maximum amount of text (measured in tokens) a model can consider in one call, including both what you send and what it generates back.

**Hallucination** — the model producing plausible but false information, especially dangerous when it invents facts (like balances) that should have come from a real data source.

**Temperature** — a setting controlling how random/creative vs. consistent/predictable a model's output is; agents doing anything factual or financial want this low.

**Policy engine** — deterministic (non-LLM) code that decides whether a proposed action is allowed, based on fixed rules — the actual safety mechanism in an agent that can take real actions.

**Idempotency** — the property that doing the same operation twice has the same effect as doing it once; critical for anything involving payments, where duplicate requests are common and dangerous.

**Webhook** — an HTTP callback a third-party service (like a payment gateway) sends to your server to notify you of an event, used as the trustworthy source of truth instead of trusting a model or browser's claim.

**Idempotency key / client reference** — a unique fingerprint you generate for "this specific action," used to detect and safely ignore duplicate requests.

**ScriptedLLM / fake model in tests** — swapping your real model-calling function for one that replays a fixed, pre-written sequence of responses, so you can test your code's logic deterministically without an actual model in the loop.

**Where to go next, once you've built the Chapter 3–7 exercises yourself:**

1. Open `CampusPool_Agent_HLD_LLD.md` and read Part 0 and Part 2 in full — it should now read as familiar rather than dense.
2. Follow `CampusPool_Build_Plan.md` starting at Phase 0 — you're now equipped for Phase 4 (the agent), which is the part that would have been intimidating before this book.
3. When you hit something this book didn't cover, the single best habit to carry forward is the one from Chapter 9: print the actual message history and read it like a transcript before assuming anything about "what the model is thinking." There is no thinking to inspect — only the text it was actually given, and the text it actually produced. Once that's genuinely intuitive, you're no longer a beginner at this.
