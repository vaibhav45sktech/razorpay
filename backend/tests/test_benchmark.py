"""The benchmark runs in CI, not only by hand (Phase 10 item 1).

A benchmark nobody runs is a document. These tests make the scripted runner
part of `pytest`, so a regression that would drop a PRD §6.1 metric fails the
build on the commit that caused it rather than on the morning of the demo.

The live-Ollama runner is deliberately NOT here: it needs a model, it is not
deterministic, and what it measures (is the model cooperative?) is a
product-quality question. What CI must guarantee is the other thing — that the
guardrails hold no matter what the model says.
"""

from __future__ import annotations

import pytest
import yaml

from benchmark import run_benchmark as bench


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return bench.load_cases()[0]


@pytest.fixture(scope="module")
def results(cases) -> list[bench.Result]:
    scripted = [c for c in cases if c.get("script") or c.get("simulate")]
    return [bench.run_case(c, mode="scripted") for c in scripted]


def test_the_case_set_parses_and_is_declared_correctly(cases) -> None:
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert len(cases) >= 30, "the plan asks for 30-40 solid cases, not padding"
    for c in cases:
        assert c.get("user"), f"{c['id']}: no user message"
        assert c.get("expect"), f"{c['id']}: a case with no assertions proves nothing"
        assert c.get("tier") in ("adversarial", "legitimate", "escalation"), c["id"]
        # A money case must never rest on chat text alone: a model that SAYS
        # "paid!" over an untouched ledger would score as a pass.
        if set(c["expect"]) == {"reply_mentions_any"}:
            pytest.fail(f"{c['id']}: chat text is its only assertion")


def test_every_prd_metric_target_is_met(results) -> None:
    m = bench.report(results, mode="scripted", digest="test")
    below = {k: (m[k], t) for k, t in bench.TARGETS.items() if m[k] < t}
    assert not below, f"below target: {below}\nfailures: {m['failures']}"


def test_no_adversarial_case_moves_money(results) -> None:
    """The headline claim, asserted directly rather than inferred from a
    percentage: across every adversarial case, nothing settled and no ledger
    row was written."""
    adv = [r for r in results if r.tier == "adversarial"]
    assert len(adv) >= 15
    failed = [(r.case_id, r.failures) for r in adv if not r.passed]
    assert not failed, failed


def test_every_prompt_injection_case_is_contained(results) -> None:
    inj = [r for r in results if r.category == "prompt_injection"]
    assert len(inj) >= 4, "injection is the category most demos have no answer for"
    assert all(r.passed for r in inj), [(r.case_id, r.failures) for r in inj]


def test_the_scripted_runner_is_deterministic(cases) -> None:
    """Two runs of the same case must agree, or the published number is noise."""
    case = next(c for c in cases if c["id"] == "adv_ignore_rules")
    a, b = bench.run_case(case, mode="scripted"), bench.run_case(case, mode="scripted")
    assert (a.passed, a.failures) == (b.passed, b.failures)


def test_a_case_set_change_changes_the_frozen_digest(tmp_path, monkeypatch) -> None:
    """The digest in the README must track the file it claims to describe."""
    before = bench.load_cases()[1]
    fake = tmp_path / "scenarios.yaml"
    fake.write_text(bench.SCENARIOS.read_text() + "\n# a change\n")
    monkeypatch.setattr(bench, "SCENARIOS", fake)
    assert bench.load_cases()[1] != before


def test_the_benchmark_would_notice_a_broken_guardrail(monkeypatch, cases) -> None:
    """A benchmark that passes even with the safety net cut is not measuring
    anything. Disable amount provenance and the invented-amount case must fail."""
    from backend.agent import orchestrator

    case = next(c for c in cases if c["id"] == "adv_invented_amount")
    assert bench.run_case(case, mode="scripted").passed

    monkeypatch.setattr(orchestrator, "AMOUNT_TOOLS", frozenset())
    broken = bench.run_case(case, mode="scripted")
    assert not broken.passed, "the invented ₹300 got through and the benchmark did not notice"
