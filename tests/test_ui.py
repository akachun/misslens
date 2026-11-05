"""T9 Streamlit: plan approval survives a refresh, then solve -> submit -> review -> diagnosis -> status.

Runs the real app script with Streamlit's AppTest, an in-memory ledger, fake LLMs and an in-memory checkpointer
(no data/ files are touched).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver
from streamlit.testing.v1 import AppTest

import misslens.ui.app as ui
from fakes import PASS, FakeRoleLLM, draft
from misslens.agents.generator import Generator
from misslens.exam_pack import load_exam_pack
from misslens.graph import Services, build_supervisor_graph
from misslens.ledger import Ledger

PACK = load_exam_pack("ccar-f")
APP = str(Path(__file__).parents[1] / "app" / "main.py")


@pytest.fixture
def res(tmp_path, monkeypatch):
    ledger = Ledger(":memory:")
    ingested = []

    class FakeIngestor:
        def ingest(self, doc):
            ingested.append(doc)

            class R:
                source, status, chunks = doc.source, "added", 2
            return R

    svc = Services(pack=PACK, ledger=ledger, make_ingestor=lambda: FakeIngestor(),
                   make_generator=lambda: Generator(PACK, ledger, gen_llm=FakeRoleLLM([draft(i % 4) for i in range(10)]),
                                                    verify_llm=FakeRoleLLM([PASS] * 10), rng=random.Random(0)))
    r = ui.Resources(PACK, ledger, svc, build_supervisor_graph(svc, checkpointer=InMemorySaver()),
                     index_stats=lambda: {"1.1": 3, "unassigned": 1}, upload_dir=tmp_path)
    r.ingested = ingested
    monkeypatch.setattr(ui, "RESOURCE_FACTORY", lambda: r)
    monkeypatch.setattr(ui, "POLL_SECONDS", 0.05)
    st.cache_resource.clear()
    yield r
    st.cache_resource.clear()
    ledger.close()


def run(x) -> AppTest:
    """x is an AppTest or a widget after an interaction (.click(), .set_value()); both have .run()."""
    at = x.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_plan_refresh_then_full_loop(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="plan_n").set_value(3)
    run(at.button(key="plan_make").click())
    tid = at.session_state["plan_thread"]
    assert at.button(key=f"approve_{tid}")                       # interrupt① shown

    # Refresh: a new session with only the URL's plan id still shows the approval screen.
    at2 = AppTest.from_file(APP, default_timeout=30)
    at2.query_params["plan"] = tid
    run(at2)
    for d in PACK.domains:
        at2.number_input(key=f"plan_{tid}_{d.id}").set_value(1 if d.id in ("d2", "d4") else 0)
    run(at2.button(key=f"approve_{tid}").click())
    quiz = at2.session_state["quiz"]
    sid = quiz["set_id"]
    assert quiz["n"] == 2

    # Solve both questions (generation runs in the background; the page polls until each is ready).
    for _ in range(2):
        for _ in range(20):
            radios = [w for w in at2.radio if w.key and w.key.startswith("ans_")]
            if radios:
                break
            run(at2)
        radios[0].set_value(0)
        run(at2.button(key=f"next_{radios[0].key[4:]}").click())
    run(at2.button(key="submit_set").click())
    assert res.ledger.get_set(sid)["status"] == "submitted"
    assert "제출 완료" in " ".join(s.value for s in at2.success)

    # Review form: answer every re-ask item, confident.
    items = at2.session_state["quiz"]["items"]
    for i in range(len(items)):
        at2.radio(key=f"re_{sid}_{i}").set_value(0)
        at2.checkbox(key=f"conf_{sid}_{i}").check()
    submit = [b for b in at2.button if "리뷰 제출" in b.label or "진단 보기" in b.label][0]
    run(submit.click())
    assert res.ledger.get_set(sid)["status"] == "reviewed"
    assert at2.session_state["quiz"]["phase"] == "diagnosis"
    assert "미끼 기준선" in [m.label for m in at2.metric]

    # Status tab shows the same coverage the ledger has.
    labels = {m.label: m.value for m in at2.metric}
    assert labels["범위"] == "2/30"


def test_plan_cancel(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    run(at.button(key="plan_make").click())
    tid = at.session_state["plan_thread"]
    run(at.button(key=f"cancel_{tid}").click())
    assert "취소" in " ".join(i.value for i in at.info)
    assert res.svc.streams == {}


def test_quick_quiz_and_mark_invalid(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="quick_n").set_value(1)
    run(at.button(key="quick_start").click())
    for _ in range(20):
        radios = [w for w in at.radio if w.key and w.key.startswith("ans_")]
        if radios:
            break
        run(at)
    radios[0].set_value(3)
    run(at.button(key=f"next_{radios[0].key[4:]}").click())
    run(at.button(key="submit_set").click())
    sid = at.session_state["quiz"]["set_id"]
    att = res.ledger.set_attempts(sid)[0]
    items = at.session_state["quiz"]["items"]
    for i in range(len(items)):
        at.radio(key=f"re_{sid}_{i}").set_value(1)
    run([b for b in at.button if b.label in ("리뷰 제출", "진단 보기")][0].click())
    if not att["is_correct"]:
        run(at.button(key=f"inv_{att['id']}").click())
        assert res.ledger.set_attempts(sid)[0]["invalid"] == 1


def test_material_paste_ingest_and_index_stats(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.text_area(key="paste_text").input("Hooks can block a tool call before it runs.")
    at.text_input(key="paste_label").input("메모")
    run(at.button(key="ingest_go").click())
    assert [d.source for d in res.ingested] == ["메모"]
    assert any("1/30" in m.value for m in at.markdown) or any("1/30" in str(t.value) for t in at.get("text"))


def test_sidebar_free_text_without_router_is_refused(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.text_input(key="nl_text").input("뭐든 해줘")
    run(at.button(key="nl_send").click())
    assert any("요청이 비어" in i.value or "실행하지" in i.value for i in at.info)
