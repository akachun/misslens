"""D52: plan history, draft answers that survive a restart, and resuming stopped generation."""

from __future__ import annotations

import random

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fakes import PASS, FakeRoleLLM, draft
from misslens.agents.generator import Generator, QuestionStream
from misslens.exam_pack import load_exam_pack
from misslens.graph import Services, build_supervisor_graph
from misslens.ledger import Answer, Ledger, LedgerError, SetStateError

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def gen(ledger, n=10):
    return Generator(PACK, ledger, gen_llm=FakeRoleLLM([draft(i % 4) for i in range(n)]),
                     verify_llm=FakeRoleLLM([PASS] * n), rng=random.Random(0))


def run(ledger, sid, n, plan=None, k=10):
    events = []
    gen(ledger, k).run_set(sid, n, events.append, plan=plan)
    return events


def test_plans_are_recorded_with_set_progress(ledger):
    sid = ledger.create_set(PACK.exam_id, 3)
    pid = ledger.record_plan(PACK.exam_id, set_id=sid, kind="plan", total=3, counts={"d2": 2, "d4": 1},
                             rationale="d2 약점", source="model", thread_id="t1")
    ledger.record_plan(PACK.exam_id, set_id=None, kind="quick", total=5, counts=None)
    (p,) = ledger.list_plans(PACK.exam_id)
    assert p["id"] == pid and p["counts"] == {"d2": 2, "d4": 1} and p["rationale"] == "d2 약점"
    assert p["set"] == {"id": sid, "status": "generating", "size": 3, "created_at": p["set"]["created_at"],
                        "ready": 0, "failed": 0, "drafts": 0}
    assert ledger.plan_for_set(sid)["counts"] == {"d2": 2, "d4": 1}
    assert [s["id"] for s in ledger.open_sets(PACK.exam_id)] == [sid]


def test_drafts_are_kept_until_submit(ledger):
    sid = ledger.create_set(PACK.exam_id, 2)
    run(ledger, sid, 2)
    q1, q2 = [q["id"] for q in ledger.set_questions(sid)]
    ledger.save_draft_answer(sid, q1, 2, 1500)
    ledger.save_draft_answer(sid, q1, 3, 1800)                      # changed mind: last answer wins
    assert ledger.draft_answers(sid) == {q1: {"question_id": q1, "chosen_index": 3, "elapsed_ms": 1800}}
    assert ledger.set_progress(sid)["drafts"] == 1
    other = ledger.create_set(PACK.exam_id, 1)
    with pytest.raises(LedgerError):
        ledger.save_draft_answer(other, q1, 0, 1)                    # not a question of that set
    ledger.submit_set(sid, [Answer(q1, 3, 1800), Answer(q2, None, None)])
    assert ledger.draft_answers(sid) == {}                           # drafts become attempts, then go away
    with pytest.raises(SetStateError):
        ledger.save_draft_answer(sid, q2, 0, 1)
    assert sid not in [s["id"] for s in ledger.open_sets(PACK.exam_id)]


def test_run_set_resumes_after_existing_questions(ledger):
    sid = ledger.create_set(PACK.exam_id, 4)
    run(ledger, sid, 2, plan={"d1": 2, "d2": 2})                     # "server restart" after 2 of 4
    first = ledger.set_questions(sid)
    assert [q["domain"] for q in first] == ["d1", "d1"]
    events = run(ledger, sid, 4, plan={"d1": 2, "d2": 2})
    qs = ledger.set_questions(sid)
    assert len(qs) == 4 and [q["seq"] for q in qs] == [0, 1, 2, 3]
    assert sorted(q["domain"] for q in qs) == ["d1", "d1", "d2", "d2"]   # quotas count what was already there
    assert len({q["task_id"] for q in qs}) == 4                      # no task asked twice
    assert sum(e.kind == "ready" for e in events) == 2


def test_supervisor_resume_generates_only_the_missing_questions(ledger):
    sid = ledger.create_set(PACK.exam_id, 3)
    ledger.record_plan(PACK.exam_id, set_id=sid, kind="plan", total=3, counts={"d3": 2, "d5": 1})
    run(ledger, sid, 1, plan={"d3": 2, "d5": 1})
    svc = Services(pack=PACK, ledger=ledger, make_generator=lambda: gen(ledger))
    g = build_supervisor_graph(svc, checkpointer=InMemorySaver())
    out = g.invoke({"intent": "resume", "args": {"set_id": sid}}, {"configurable": {"thread_id": "r"}})
    assert "남은 2문제" in out["message"]
    svc.streams[sid].join(timeout=10)
    assert sorted(q["domain"] for q in ledger.set_questions(sid)) == ["d3", "d3", "d5"]
    out = g.invoke({"intent": "resume", "args": {"set_id": sid}}, {"configurable": {"thread_id": "r2"}})
    assert "이어서 풉니다" in out["message"]                          # complete: nothing to generate


def test_generate_records_quick_and_plan_sets(ledger):
    svc = Services(pack=PACK, ledger=ledger, make_generator=lambda: gen(ledger))
    g = build_supervisor_graph(svc, checkpointer=InMemorySaver())
    out = g.invoke({"intent": "quiz", "args": {"count": 2, "domain": "d2"}}, {"configurable": {"thread_id": "q"}})
    svc.streams[out["set_id"]].join(timeout=10)
    p = ledger.plan_for_set(out["set_id"])
    assert p["kind"] == "quick" and p["counts"] == {"d2": 2} and p["thread_id"] == "q"
    assert ledger.list_plans(PACK.exam_id) == []                     # quick sets are not shown as plans


def test_set_history_shows_every_origin(ledger):
    """D53: planned, quick, free-text and CLI sets all appear in the history with where they came from."""
    def submitted(kind, thread_id):
        sid = ledger.create_set(PACK.exam_id, 1)
        if kind:
            ledger.record_plan(PACK.exam_id, set_id=sid, kind=kind, total=1, counts={"d1": 1}, thread_id=thread_id)
        run(ledger, sid, 1)
        (q,) = ledger.set_questions(sid)
        ledger.submit_set(sid, [Answer(q["id"], 0, 100)])
        return sid

    ids = {"plan": submitted("plan", "ui-plan-1"), "ask": submitted("quick", "ui-ask-1"),
           "quick": submitted("quick", "ui-quiz-1"), "cli": submitted("quick", "cli-quiz"),
           "unknown": submitted(None, None)}
    hist = ledger.list_sets(PACK.exam_id)
    assert {h["id"]: h["origin"] for h in hist} == {v: k for k, v in ids.items()}
    assert [h["id"] for h in hist] == sorted(ids.values(), reverse=True)
    open_one = ledger.create_set(PACK.exam_id, 1)
    assert open_one not in [h["id"] for h in ledger.list_sets(PACK.exam_id)]   # open sets are in "이어 풀기"
