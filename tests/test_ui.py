"""T9 Streamlit: 새로고침해도 계획 승인이 남고, 이어서 풀이 -> 제출 -> 리뷰 -> 진단 -> 현황.

Streamlit의 AppTest로 실제 앱 스크립트를 돌린다. 메모리 ledger, 가짜 LLM, 메모리 checkpointer를 쓴다
(data/ 파일은 건드리지 않는다).
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
    monkeypatch.setattr(ui, "POLL_MODE", "rerun")       # AppTest는 run_every fragment를 구동할 수 없다
    st.cache_resource.clear()
    yield r
    st.cache_resource.clear()
    ledger.close()


def run(x) -> AppTest:
    """x는 AppTest이거나 상호작용(.click(), .set_value()) 뒤의 위젯이다. 둘 다 .run()이 있다."""
    at = x.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_plan_refresh_then_full_loop(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="plan_n").set_value(3)
    run(at.button(key="plan_make").click())
    tid = at.session_state["plan_thread"]
    assert at.button(key=f"approve_{tid}")                       # interrupt① 표시

    # 새로고침: URL의 계획 id만 가진 새 세션도 승인 화면을 여전히 보여 준다.
    at2 = AppTest.from_file(APP, default_timeout=30)
    at2.query_params["plan"] = tid
    run(at2)
    for d in PACK.domains:
        at2.number_input(key=f"plan_{tid}_{d.id}").set_value(1 if d.id in ("d2", "d4") else 0)
    run(at2.button(key=f"approve_{tid}").click())
    quiz = at2.session_state["quiz"]
    sid = quiz["set_id"]
    assert quiz["n"] == 2

    # 두 문제를 푼다(생성은 백그라운드에서 돌고, 페이지는 각 문제가 ready가 될 때까지 폴링한다).
    for _ in range(2):
        for _ in range(20):
            radios = [w for w in at2.radio if w.key and w.key.startswith("ans_")]
            if radios:
                break
            run(at2)
        assert res.ledger.get_set(sid)["status"] == "in_progress"     # 표시 -> 시작, CLI와 같다
        radios[0].set_value(0)
        run(at2.button(key=f"next_{radios[0].key[4:]}").click())
    run(at2.button(key="submit_set").click())
    assert res.ledger.get_set(sid)["status"] == "submitted"
    assert "제출 완료" in " ".join(s.value for s in at2.success)

    # 리뷰 폼: 재질문 항목에 모두 확신을 갖고 답한다.
    items = at2.session_state["quiz"]["items"]
    for i in range(len(items)):
        at2.radio(key=f"re_{sid}_{i}").set_value(0)
        at2.checkbox(key=f"conf_{sid}_{i}").check()
    submit = [b for b in at2.button if "리뷰 제출" in b.label or "진단 보기" in b.label][0]
    run(submit.click())
    assert res.ledger.get_set(sid)["status"] == "reviewed"
    if at2.session_state["quiz"]["phase"] == "self_check":         # 실수: 판정 전에 라벨을 붙인다
        for w in [w for w in at2.radio if w.key and w.key.startswith("self_")]:
            w.set_value("concept")
        run(at2.button(key=f"self_done_{sid}").click())
        assert len(res.ledger.labeled_misses(PACK.exam_id)) >= 1
    assert at2.session_state["quiz"]["phase"] == "diagnosis"
    assert "미끼 기준선" in [m.label for m in at2.metric]

    # 현황 탭은 ledger와 같은 커버리지를 보여 준다.
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
    if at.session_state["quiz"]["phase"] == "self_check":
        run(at.button(key=f"self_done_{sid}").click())             # 라벨 건너뜀: 아무것도 저장하지 않는다
        assert res.ledger.labeled_misses(PACK.exam_id) == []
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
    at.text_area(key="nl_text").input("뭐든 해줘")
    run([b for b in at.button if b.label == "보내기"][0].click())
    assert any("요청이 비어" in i.value or "실행하지" in i.value for i in at.info)


def test_coach_keeps_stored_explanation_and_answers_follow_ups(res):
    """D47: 저장된 설명은 그대로 있고, 코치 답과 후속 대화가 출처 링크와 함께 아래에 나타난다."""
    res.explainer = FakeRoleLLM(["코치 첫 해설", "후속 답변입니다."])
    res.svc.index = None
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="quick_n").set_value(1)
    run(at.button(key="quick_start").click())
    for _ in range(20):
        radios = [w for w in at.radio if w.key and w.key.startswith("ans_")]
        if radios:
            break
        run(at)
    q = res.ledger.set_questions(at.session_state["quiz"]["set_id"])[0]
    radios[0].set_value((q["payload"]["answer_index"] + 1) % 4)          # 오답
    run(at.button(key=f"next_{radios[0].key[4:]}").click())
    run(at.button(key="submit_set").click())
    sid = at.session_state["quiz"]["set_id"]
    for i in range(len(at.session_state["quiz"]["items"])):
        at.radio(key=f"re_{sid}_{i}").set_value(1)
    run([b for b in at.button if b.label in ("리뷰 제출", "진단 보기")][0].click())
    if at.session_state["quiz"]["phase"] == "self_check":
        run(at.button(key=f"self_done_{sid}").click())
    att = res.ledger.set_attempts(sid)[0]
    run(at.button(key=f"btn_exp_{att['id']}").click())
    texts = [m.value for m in at.markdown]
    assert "**해설**" in texts and "**코치 해설**" in texts             # 둘 다 유지
    assert draft(0)["explanation_native"] in texts                     # 저장된 설명이 화면에 여전히 있다
    assert "코치 첫 해설" in texts
    at.text_area(key=f"q_{att['id']}").input("tool_choice는 뭐예요?")
    run([b for b in at.button if b.label == "질문하기"][0].click())
    assert any(c.name == "user" for c in at.chat_message) and any(c.name == "assistant" for c in at.chat_message)


def test_source_links_render_urls_as_links():
    out = ui.source_links(["https://anthropic-partners.skilljar.com/claude-101/123", "guide.pdf p.13"])
    assert out == "[claude-101/123](https://anthropic-partners.skilljar.com/claude-101/123) · `guide.pdf p.13`"


def _diagnosis(at, res, wrong: bool):
    at.number_input(key="quick_n").set_value(1)
    run(at.button(key="quick_start").click())
    for _ in range(20):
        radios = [w for w in at.radio if w.key and w.key.startswith("ans_")]
        if radios:
            break
        run(at)
    q = res.ledger.set_questions(at.session_state["quiz"]["set_id"])[0]
    a = q["payload"]["answer_index"]
    radios[0].set_value((a + 1) % 4 if wrong else a)
    run(at.button(key=f"next_{radios[0].key[4:]}").click())
    run(at.button(key="submit_set").click())
    sid = at.session_state["quiz"]["set_id"]
    for i in range(len(at.session_state["quiz"]["items"])):
        at.radio(key=f"re_{sid}_{i}").set_value(1)
    run([b for b in at.button if b.label in ("리뷰 제출", "진단 보기")][0].click())
    if at.session_state["quiz"]["phase"] == "self_check":
        run(at.button(key=f"self_done_{sid}").click())
    return res.ledger.set_attempts(sid)[0]


@pytest.mark.parametrize("wrong", [True, False])
def test_question_box_is_there_before_any_explanation(res, wrong):
    """①: 실수를 직접 묻는다. ②: 정답으로 답한 문제에 대해 묻는다."""
    res.explainer = FakeRoleLLM(["바로 답합니다."])
    res.svc.index = None
    at = run(AppTest.from_file(APP, default_timeout=30))
    att = _diagnosis(at, res, wrong)
    assert bool(att["is_correct"]) is (not wrong)
    at.text_area(key=f"q_{att['id']}").input("이 보기는 왜 틀렸어요?")
    run([b for b in at.button if b.label == "질문하기"][0].click())
    msgs = [c for c in at.chat_message]
    assert [c.name for c in msgs] == ["user", "assistant"]
    assert "**코치 해설**" not in [m.value for m in at.markdown]         # 설명을 요청하지 않았다


def test_sidebar_request_is_multiline_form_and_shows_what_was_sent(res):
    """긴 요청도 들어가고(text_area), 폼(Ctrl/⌘+Enter 또는 버튼)으로 보낸다. 보낸 텍스트가 다시 표시된다."""
    long = "다음 주 시험이라 도구 설계와 MCP 영역 위주로 문제를 많이 풀고 싶은데, 오늘은 어떤 순서로 공부하면 좋을지 계획을 세워줘"
    at = run(AppTest.from_file(APP, default_timeout=30))
    assert at.text_area(key="nl_text").value == ""
    at.text_area(key="nl_text").input(long)
    run([b for b in at.button if b.label == "보내기"][0].click())
    assert at.session_state["nl_last"] == long
    assert any(long in m.value for m in at.sidebar.markdown)
    # clear_on_submit과 Ctrl/⌘+Enter는 AppTest가 재현하지 않는 브라우저 동작이다. 실제 브라우저에서 확인했다.
    assert any("Ctrl+Enter" in c.value for c in at.sidebar.caption)


def test_sidebar_width_applies_only_while_expanded():
    """접힌 사이드바에 고정 너비를 주면 빈 띠가 남고 메인 영역이 좁게 유지됐다(시연 피드백).
    레이아웃 자체는 브라우저에서 확인했다: 접힘 -> 메인 영역이 0에서 시작해 창 전체에 걸친다."""
    assert "[data-collapsed='false']" in ui.SIDEBAR_CSS
    assert "width:380px" not in ui.SIDEBAR_CSS.replace("min-width:380px", "")


def test_draft_answers_survive_a_new_session_and_open_sets_offer_resume(res):
    """D52: 한 문제에 답하고 '재시작'(새 AppTest 세션)하면 세트가 제안되고 2번 문제부터 이어진다."""
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="quick_n").set_value(2)
    run(at.button(key="quick_start").click())
    for _ in range(20):
        radios = [w for w in at.radio if w.key and w.key.startswith("ans_")]
        if radios:
            break
        run(at)
    first_qid = int(radios[0].key[4:])
    radios[0].set_value(1)
    run(at.button(key=f"next_{first_qid}").click())
    sid = at.session_state["quiz"]["set_id"]
    assert res.ledger.draft_answers(sid)[first_qid]["chosen_index"] == 1
    res.svc.streams[sid].join(timeout=10)

    at2 = run(AppTest.from_file(APP, default_timeout=30))            # URL 없음: 학습 탭이 열린 세트를 나열한다
    assert at2.button(key=f"resume_{sid}")
    run(at2.button(key=f"resume_{sid}").click())
    radios = [w for w in at2.radio if w.key and w.key.startswith("ans_")]
    assert radios and int(radios[0].key[4:]) != first_qid          # 답하지 않은 문제부터 이어진다
    assert at2.session_state["quiz"]["answers"][first_qid]["chosen_index"] == 1


def test_plan_history_lists_approved_plan_with_its_set(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    at.number_input(key="plan_n").set_value(2)
    run(at.button(key="plan_make").click())
    tid = at.session_state["plan_thread"]
    run(at.button(key=f"approve_{tid}").click())
    sid = at.session_state["quiz"]["set_id"]
    res.svc.streams[sid].join(timeout=10)
    run(at.button(key="plan_reset").click())
    (p,) = res.ledger.list_plans(PACK.exam_id)
    assert p["set_id"] == sid and p["thread_id"] == tid
    assert any(f"세트 #{sid}" in e.label for e in at.expander)
    assert at.button(key=f"plan_go_{p['id']}")


def test_past_sets_panel_reopens_a_quick_set(res):
    """D53: '바로 풀기'로 만든 세트는 제출 후 목록에 나오고 리뷰/진단으로 다시 열린다."""
    at = run(AppTest.from_file(APP, default_timeout=30))
    att = _diagnosis(at, res, wrong=True)
    sid = att["set_id"]
    run(at.button(key="new_set").click())                            # 목록으로 돌아감
    assert at.session_state.get("quiz") is None
    assert at.button(key=f"past_{sid}").label == "진단 보기"
    assert any(f"세트 #{sid}" in m.value and "바로 풀기" in m.value for m in at.markdown)
    run(at.button(key=f"past_{sid}").click())
    assert at.session_state["quiz"]["set_id"] == sid and at.session_state["quiz"]["phase"] == "diagnosis"


def test_vocab_panel_and_word_book(res):
    """D54: 진단이 백그라운드에서 세트의 단어 목록을 만든다. 단어장 탭은 단어를 나열하고 아는 단어를 숨긴다."""
    res.vocab_llm = FakeRoleLLM([{"items": [{"q": 1, "term": "must not store passwords", "kind": "condition",
                                             "meaning": "비밀번호를 저장하면 안 된다", "role": "정답을 가른 조건"}]}])
    at = run(AppTest.from_file(APP, default_timeout=30))
    _diagnosis(at, res, wrong=True)
    sid = at.session_state["quiz"]["set_id"]
    for _ in range(20):                                              # 백그라운드 작업, 그다음 rerun
        if res.ledger.set_vocab(sid):
            break
        run(at)
    run(at)
    assert any("must not store passwords" in m.value for m in at.markdown)
    key = f"vk_{sid}_must not store passwords"
    run(at.checkbox(key=key).check())
    assert res.ledger.vocab_book(PACK.exam_id)[0]["known"]
    assert not any("must not store passwords" in m.value and "회(세트" in m.value for m in at.markdown)  # 단어장에서 숨김
    run(at.checkbox(key="vb_hide").uncheck())
    assert any("must not store passwords" in m.value and "1회(세트 1개)" in m.value for m in at.markdown)


def test_vocab_text_keeps_quotes_but_blocks_tags():
    """단어장 원문 문장 속 코드 블록에 `&quot;`가 그대로 보이던 문제(시연 캡처에서 발견)."""
    from misslens.ui.app import md_text

    assert md_text('claude "Scan the codebase"') == 'claude "Scan the codebase"'
    assert md_text("<script>x</script> & y") == "&lt;script&gt;x&lt;/script&gt; &amp; y"


def test_background_signature_and_activity_lines(res):
    """D55: 상태 fragment는 이 시그니처가 바뀔 때만 페이지를 rerun하고, 작업을 한 줄에 나열한다."""
    import threading

    idle = ui.background_signature(res)
    assert ui.activity_lines(res) == []
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, daemon=True)
    t.start()
    res.jobs["vocab-7"] = {"thread": t, "out": None, "error": None}
    res.jobs["ui-ask-1"] = {"thread": t, "out": None, "error": None}
    busy = ui.background_signature(res)
    assert busy != idle and ui.activity_lines(res) == ["단어장 만들기", "요청 처리"]
    gate.set()
    t.join(timeout=5)
    assert ui.background_signature(res) != busy and ui.activity_lines(res) == []


def test_usage_help_is_on_the_page(res):
    at = run(AppTest.from_file(APP, default_timeout=30))
    assert any(e.label == "사용법" for e in at.expander)
