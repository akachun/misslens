"""T8: domain_summary, plan quotas, Planner (interrupt①), Reporter, Supervisor routing over all agents."""

from __future__ import annotations

import random

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fakes import PASS, FakeRoleLLM, draft
from misslens.agents.collector import SourceList
from misslens.agents.generator import Generator
from misslens.agents.planner import (
    Plan, PlanError, build_planner_graph, draft_plan, rule_plan, summaries, validate_counts,
)
from misslens.agents.reporter import build_report
from misslens.exam_pack import load_exam_pack
from misslens.graph import Services, build_supervisor_graph
from misslens.ledger import Answer, Ledger, domain_summary
from misslens.llm import LLMCallError
from test_collector import HOST, RAW, FakeFetcher, course_pages

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def cfg(tid):
    return {"configurable": {"thread_id": tid}, "recursion_limit": 200}


def fake_generator(ledger, n=10):
    return Generator(PACK, ledger, gen_llm=FakeRoleLLM([draft(i % 4) for i in range(n)]),
                     verify_llm=FakeRoleLLM([PASS] * n), rng=random.Random(0))


def answered_set(ledger, task_ids, correct):
    sid = ledger.create_set(PACK.exam_id, len(task_ids))
    qids = [ledger.add_question(sid, seq=i, task_id=t, domain=PACK.domain_of(t).id, blueprint_version=PACK.version,
                                payload={**draft(0), "task_id": t, "domain": PACK.domain_of(t).id,
                                         "scenario_id": None, "grounding": "retrieved"})
            for i, t in enumerate(task_ids)]
    ledger.start_set(sid)
    ledger.submit_set(sid, [Answer(q, 0 if c else 1, 1000) for q, c in zip(qids, correct)])
    return sid


# --- ledger and planner ----------------------------------------------------------------------


def test_domain_summary_counts_coverage_and_accuracy(ledger):
    answered_set(ledger, ["1.1", "1.2", "2.1"], [True, False, False])
    s = domain_summary(ledger, PACK.exam_id, {t: PACK.domain_of(t).id for t in PACK.task_ids})
    assert (s["d1"].tasks, s["d1"].asked, s["d1"].correct, s["d1"].attempts) == (7, 2, 1, 2)
    assert s["d2"].accuracy == pytest.approx(1 / 3) and s["d5"].attempts == 0
    assert s["d5"].unasked_ratio == 1.0


def test_rule_plan_sums_and_prefers_weak_uncovered_domains(ledger):
    answered_set(ledger, PACK.domains[0].tasks and [t.id for t in PACK.domains[0].tasks], [True] * 7)
    plan = rule_plan(PACK, summaries(PACK, ledger), 10)
    assert sum(plan.counts.values()) == 10 and set(plan.counts) == {d.id for d in PACK.domains}
    assert plan.counts["d1"] < plan.counts["d3"]  # d1 fully covered and all correct


def test_validate_counts_rejects_bad_edits():
    with pytest.raises(PlanError, match="알 수 없는"):
        validate_counts(PACK, {"d9": 1})
    with pytest.raises(PlanError, match="0 이상"):
        validate_counts(PACK, {"d1": -1, "d2": 3})
    with pytest.raises(PlanError, match="1~30"):
        validate_counts(PACK, {"d1": 0})
    with pytest.raises(PlanError, match="다릅니다"):
        validate_counts(PACK, {"d1": 3}, total=5)
    assert validate_counts(PACK, {"d2": "4"}) == {"d1": 0, "d2": 4, "d3": 0, "d4": 0, "d5": 0}


def test_model_plan_used_when_valid_else_rule_fallback(ledger):
    good = {"counts": {"d1": 2, "d2": 2, "d3": 2, "d4": 2, "d5": 2}, "rationale_ko": "고르게 냈습니다."}
    plan, table = draft_plan(PACK, ledger, 10, FakeRoleLLM([good]))
    assert plan.source == "model" and plan.rationale == "고르게 냈습니다." and len(table) == 5
    bad = {"counts": {"d1": 9, "d2": 9}, "rationale_ko": "x"}
    assert draft_plan(PACK, ledger, 10, FakeRoleLLM([bad]))[0].source == "rule"
    assert draft_plan(PACK, ledger, 10, FakeRoleLLM([LLMCallError("planner", [])]))[0].source == "rule"


def test_planner_graph_interrupt_edit_reask_and_approve(ledger):
    g = build_planner_graph(PACK, ledger, None, checkpointer=InMemorySaver())
    out = g.invoke({"total": 5}, cfg("p1"))
    payload = out["__interrupt__"][0].value
    assert payload["kind"] == "plan" and sum(payload["plan"]["counts"].values()) == 5 and payload["error"] is None
    out = g.invoke(Command(resume={"action": "approve", "counts": {"d9": 2}}), cfg("p1"))
    assert "알 수 없는" in out["__interrupt__"][0].value["error"]          # asked again with the reason
    out = g.invoke(Command(resume={"action": "approve", "counts": {"d2": 3, "d4": 1}}), cfg("p1"))
    assert out["plan_status"] == "approved"
    plan = Plan.model_validate(out["plan"])
    assert plan.source == "learner" and plan.total == 4 and plan.counts["d2"] == 3


def test_planner_cancel(ledger):
    g = build_planner_graph(PACK, ledger, None, checkpointer=InMemorySaver())
    g.invoke({"total": 5}, cfg("p2"))
    assert g.invoke(Command(resume={"action": "cancel"}), cfg("p2"))["plan_status"] == "cancelled"


# --- generator quotas and reporter ---------------------------------------------------------------


def test_generator_respects_domain_plan(ledger):
    sid = ledger.create_set(PACK.exam_id, 3)
    events = []
    fake_generator(ledger).run_set(sid, 3, events.append, plan={"d2": 2, "d5": 1})
    domains = [e.question.domain for e in events if e.kind == "ready"]
    assert sorted(domains) == ["d2", "d2", "d5"] and events[-1].kind == "done"


def test_report_matches_ledger(ledger):
    answered_set(ledger, ["1.1", "2.1"], [True, False])
    r = build_report(PACK, ledger)
    assert r.asked == ["1.1", "2.1"] and r.tasks_total == 30 and r.coverage_ratio == pytest.approx(2 / 30)
    assert [d["domain"] for d in r.domains] == ["d1", "d2", "d3", "d4", "d5"]
    assert r.as_dict()["avg_time_ms"] == 1000


# --- supervisor ---------------------------------------------------------------------------------


def services(ledger, router=None, fetchers=None, ingested=None):
    class FakeIngestor:
        def ingest(self, doc):
            ingested.append(doc)

            class R:
                source, status, chunks = doc.source, "added", 1
            return R

    return Services(pack=PACK, ledger=ledger, make_generator=lambda: fake_generator(ledger), router_llm=router,
                    make_ingestor=(lambda: FakeIngestor()) if ingested is not None else None,
                    sources=SourceList.model_validate(RAW) if fetchers else None, fetchers=fetchers or {})


def drain(svc, set_id):
    stream = svc.streams[set_id]
    out = []
    while True:
        ev = stream.next(timeout=10)
        out.append(ev)
        if ev.kind in ("done", "stopped"):
            return out


def test_status_command_goes_to_reporter_without_model(ledger):
    g = build_supervisor_graph(services(ledger), checkpointer=InMemorySaver())
    out = g.invoke({"intent": "status"}, cfg("s1"))
    assert out["report"]["tasks_total"] == 30


def test_plan_quiz_interrupts_then_generates_approved_plan(ledger):
    svc = services(ledger)
    g = build_supervisor_graph(svc, checkpointer=InMemorySaver())
    out = g.invoke({"intent": "plan_quiz", "args": {"count": 3}}, cfg("s2"))
    assert out["__interrupt__"][0].value["kind"] == "plan" and "set_id" not in out
    out = g.invoke(Command(resume={"action": "approve", "counts": {"d3": 2, "d4": 1}}), cfg("s2"))
    evs = drain(svc, out["set_id"])
    assert sorted(e.question.domain for e in evs if e.kind == "ready") == ["d3", "d3", "d4"]


def test_plan_cancel_makes_no_set(ledger):
    svc = services(ledger)
    g = build_supervisor_graph(svc, checkpointer=InMemorySaver())
    g.invoke({"intent": "plan_quiz"}, cfg("s3"))
    out = g.invoke(Command(resume={"action": "cancel"}), cfg("s3"))
    assert out.get("set_id") is None and "취소" in out["message"] and svc.streams == {}


def test_free_text_routed_by_model_to_domain_quiz(ledger):
    router = FakeRoleLLM([{"intent": "quiz", "domain": "d2", "count": 2, "confidence": 0.9}])
    svc = services(ledger, router)
    out = build_supervisor_graph(svc, checkpointer=InMemorySaver()).invoke({"text": "도구 설계 2문제"}, cfg("s4"))
    assert [e.question.domain for e in drain(svc, out["set_id"]) if e.kind == "ready"] == ["d2", "d2"]
    assert "도구 설계 2문제" in router.prompts[0][1].content


@pytest.mark.parametrize("decision", [
    {"intent": "quiz", "domain": None, "count": None, "confidence": 0.3},
    {"intent": "unknown", "domain": None, "count": None, "confidence": 0.9},
    LLMCallError("router", []),
])
def test_unclear_free_text_runs_nothing(ledger, decision):
    svc = services(ledger, FakeRoleLLM([decision]))
    out = build_supervisor_graph(svc, checkpointer=InMemorySaver()).invoke({"text": "음..."}, cfg("s5"))
    assert out.get("set_id") is None and "실행하지 않았습니다" in out["message"] and svc.streams == {}


def test_collect_route_waits_for_login_then_ingests(ledger, tmp_path, monkeypatch):
    import misslens.agents.collector.graph as cg

    monkeypatch.setattr(cg, "COLLECTED_DIR", tmp_path)
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)
    fetcher = FakeFetcher(course_pages(), expire_once={f"{HOST}/course-a/101"})
    ingested = []
    g = build_supervisor_graph(services(ledger, fetchers={"academy": fetcher}, ingested=ingested),
                               checkpointer=InMemorySaver())
    out = g.invoke({"intent": "collect"}, cfg("s6"))
    assert out["__interrupt__"][0].value["kind"] == "login"          # interrupt② surfaces through the parent
    out = g.invoke(Command(resume="done"), cfg("s6"))
    assert [d.source for d in ingested] == [f"{HOST}/course-a/101", f"{HOST}/page/notes"]
    assert all(d.source_type == "collect" for d in ingested) and fetcher.closed >= 2


def test_submit_route_grades_set(ledger):
    sid = ledger.create_set(PACK.exam_id, 1)
    qid = ledger.add_question(sid, seq=0, task_id="1.1", domain="d1", blueprint_version=PACK.version,
                              payload={**draft(2), "task_id": "1.1", "domain": "d1", "scenario_id": None,
                                       "grounding": "retrieved"})
    ledger.start_set(sid)
    out = build_supervisor_graph(services(ledger), checkpointer=InMemorySaver()).invoke(
        {"intent": "submit", "args": {"set_id": sid, "answers": [
            {"question_id": qid, "chosen_index": 2, "elapsed_ms": 500}]}}, cfg("s7"))
    assert out["score"] == {"total": 1, "correct": 1}


def test_cli_plan_flow_approve_edit_solve_and_review(ledger):
    """CLI `plan`: interrupt① answered at the prompt (edit counts), then the set is solved and reviewed."""
    import io

    from rich.console import Console

    from misslens.cli import CLI, run_supervisor

    answers = iter(["e", "0 2 0 0 0", "A", "B", "n"] + ["A", "y"] * 4)
    out = io.StringIO()
    cli = CLI(PACK, ledger, console=Console(file=out, width=200), ask=lambda _: next(answers))
    res = run_supervisor(cli, {"intent": "plan_quiz", "args": {"count": 3}}, svc=services(ledger),
                         checkpointer=InMemorySaver())
    text = out.getvalue()
    assert "오늘의 계획" in text and res["plan"]["counts"]["d2"] == 2 and res["plan"]["source"] == "learner"
    assert ledger.get_set(res["set_id"])["status"] in ("submitted", "reviewed")
    assert "제출 완료" in text


def test_committed_graph_diagram_matches_compiled_graph():
    """docs/graph.mmd and the README diagram are generated from the graph; regenerate with scripts/draw_graph.py."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("draw_graph", root / "scripts" / "draw_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = mod.mermaid().strip()
    assert (root / "docs" / "graph.mmd").read_text(encoding="utf-8").strip() == fresh
    assert fresh in (root / "README.md").read_text(encoding="utf-8")
    assert "interrupt①" in fresh and "interrupt②" in fresh
