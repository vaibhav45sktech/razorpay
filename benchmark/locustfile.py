"""Load profiles (master build plan Phase 7 Step 4, Production Readiness §3.6).

Two profiles, deliberately kept apart, because they have different ceilings
and mixing them produces a number that describes neither:

    ReadUser   GET /api/state, /api/audit, /api/card, /api/pool - plain SQLite
               reads behind derived-balance queries. Should sustain meaningful
               concurrency on a laptop.

    ChatUser   POST /api/chat - each call is several inference passes against
               ONE local Ollama process. This does NOT scale, and the honest
               way to report it is a measured number rather than an omission.
               It is a known consequence of the no-external-API constraint
               (every request is served by one model on one machine), not a
               bug to tune away. Anything beyond a couple of concurrent chat
               users queues; latency rises roughly linearly.

Run one at a time, never together:

    locust -f benchmark/locustfile.py --host http://localhost:8000 \
           --users 50 --spawn-rate 10 --run-time 2m ReadUser
    locust -f benchmark/locustfile.py --host http://localhost:8000 \
           --users 3  --spawn-rate 1  --run-time 3m ChatUser

Target concurrency: TODO — confirm expected demo load with the product owner.
A figure invented here ("1,000 concurrent students") would be exactly the kind
of unevidenced claim the rest of this project refuses to make. What IS recorded
in the README is what the numbers actually were on the demo machine.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, events, task

# Read-only chat prompts. No amount is stated in any of them on purpose: a load
# test must never be able to create an intent, or a benchmark run would leave
# money-shaped rows in a database someone later demos from.
SAFE_PROMPTS = [
    "What's my balance?",
    "How much have I spent this month?",
    "What food offers do you have?",
    "How does the pool work?",
    "Why this month for my draw?",
]


@events.test_start.add_listener
def _warn(environment, **_kw) -> None:
    print("\n  Load profiles are single-purpose: run ReadUser OR ChatUser, not both.\n"
          "  /api/chat is bounded by one local model and is expected to queue.\n")


class _Base(HttpUser):
    abstract = True
    user_id: str = ""

    def on_start(self) -> None:
        with self.client.get("/api/users", catch_response=True) as r:
            if r.status_code != 200:
                r.failure("could not list users; is the backend seeded?")
                self.environment.runner.quit()
                return
            users = r.json().get("users") or []
            if not users:
                r.failure("no demo users; run POST /debug/seed")
                self.environment.runner.quit()
                return
            self.user_id = users[0]["user_id"]


class ReadUser(_Base):
    """The profile the app actually spends its time in."""

    wait_time = between(0.5, 2.0)

    @task(5)
    def state(self) -> None:
        self.client.get(f"/api/state/{self.user_id}", name="/api/state/{id}")

    @task(2)
    def audit(self) -> None:
        self.client.get(f"/api/audit?user_id={self.user_id}&limit=80", name="/api/audit")

    @task(2)
    def card(self) -> None:
        self.client.get(f"/api/card/{self.user_id}", name="/api/card/{id}")

    @task(2)
    def autopilot(self) -> None:
        self.client.get(f"/api/plan/{self.user_id}", name="/api/plan/{id}")
        self.client.get(f"/api/pool/{self.user_id}", name="/api/pool/{id}")

    @task(1)
    def offers(self) -> None:
        self.client.get(f"/api/spend/{self.user_id}", name="/api/spend/{id}")

    @task(1)
    def health(self) -> None:
        self.client.get("/health")


class ChatUser(_Base):
    """The profile with a hard ceiling. Expect queueing, and report it."""

    wait_time = between(5.0, 12.0)     # a real student does not chat in a tight loop

    @task
    def chat(self) -> None:
        self.client.post(
            "/api/chat",
            json={"user_id": self.user_id, "message": random.choice(SAFE_PROMPTS), "history": []},
            name="/api/chat",
            timeout=120,
        )
