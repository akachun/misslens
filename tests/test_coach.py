"""D44: tool-using coach (ReAct subgraph) for miss explanations."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from fakes import FakeRoleLLM, draft
from misslens.agents.grader.coach import MAX_TOOL_ROUNDS, coach_explain, make_tools
from misslens.agents.grader.review import DiagnosisRow
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Answer, Ledger
from misslens.llm import LLMCallError
from misslens.retrieval import ChunkIndex, IndexedChunk
from test_retrieval import FakeEmbedder

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


@pytest.fixture
def index(tmp_path):
    idx = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    idx.add([IndexedChunk("Set tool_choice to force the model to call one specific tool.", "lesson/tool-choice",
                          "h1", ["2.3"]),
             IndexedChunk("Prompt caching reduces cost for repeated long prompts.", "lesson/caching", "h2", ["4.5"])])
    return idx


def row(miss_type="reading", phrase="must not store passwords"):
    q = {**draft(0), "task_id": "2.3", "domain": "d2", "scenario_id": None, "grounding": "retrieved"}
    return DiagnosisRow(seq=0, attempt_id=1, question=q, chosen_index=1, is_correct=False, is_decoy=False,
                        miss_type=miss_type, missed_phrase=phrase, elapsed_ms=1000)


def call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}])


def test_coach_searches_checks_history_then_explains(ledger, index):
    llm = FakeRoleLLM([call("search_material", {"query": "tool_choice force", "task_id": "2.3"}, 1),
                       call("learner_history", {"task_id": "2.3"}, 2),
                       "tool_choice로 특정 도구를 강제합니다. 지문의 조건을 놓쳤습니다. (출처: lesson/tool-choice)"])
    res = coach_explain(llm, PACK, row(), ledger=ledger, index=index)
    assert res.used_tools and res.text.startswith("tool_choice로")
    assert res.tool_calls == ["search_material(query='tool_choice force', task_id='2.3')",
                              "learner_history(task_id='2.3')"]
    assert res.sources[0] == "lesson/tool-choice"
    assert llm.bound_tools == ["search_material", "learner_history"]
    # the tool results went back to the model as reference data
    last_prompt = llm.prompts[-1]
    tool_texts = [m.content for m in last_prompt if m.type == "tool"]
    assert "<reference_material>" in tool_texts[0] and "Task 2.3" in tool_texts[1]
    assert "Diagnosis: a READING miss" in last_prompt[0].content


def test_learner_history_reads_ledger_stats(ledger):
    sid = ledger.create_set(PACK.exam_id, 1)
    qid = ledger.add_question(sid, seq=0, task_id="2.3", domain="d2", blueprint_version=PACK.version,
                              payload={**draft(0), "task_id": "2.3", "domain": "d2", "scenario_id": None,
                                       "grounding": "retrieved"})
    ledger.start_set(sid)
    ledger.submit_set(sid, [Answer(qid, 1, 500)])
    tools = {t.name: t for t in make_tools(PACK, ledger, None)}
    out = tools["learner_history"].invoke({"task_id": "2.3"})
    assert "Recent attempts 1, correct 0" in out and "Distribute tools" in out
    assert "Unknown task id" in tools["learner_history"].invoke({"task_id": "9.9"})
    assert tools["search_material"].invoke({"query": "x"}) == "No study material is indexed yet."


def test_bad_tool_args_do_not_break_the_loop(ledger, index):
    llm = FakeRoleLLM([call("learner_history", {"wrong": 1}, 1), "해설입니다."])
    res = coach_explain(llm, PACK, row(), ledger=ledger, index=index)
    assert res.text == "해설입니다."
    err = [m for m in llm.prompts[-1] if m.type == "tool"][0]
    assert "error" in str(err.content).lower()


def test_too_many_tool_rounds_fall_back_to_plain_explanation(ledger, index):
    calls = [call("search_material", {"query": f"q{i}"}, i) for i in range(MAX_TOOL_ROUNDS + 1)]
    llm = FakeRoleLLM(calls + ["일반 해설"])
    res = coach_explain(llm, PACK, row(), ledger=ledger, index=index)
    assert res.text == "일반 해설" and not res.used_tools


def test_model_failure_falls_back_to_stored_explanation(ledger, index):
    llm = FakeRoleLLM([LLMCallError("explainer", []), LLMCallError("explainer", [])])
    res = coach_explain(llm, PACK, row(), ledger=ledger, index=index)
    assert res.text == draft(0)["explanation_native"]


# --- follow-up conversation (D47) -------------------------------------------------------------


def test_follow_up_questions_continue_the_thread(ledger, index):
    from langgraph.checkpoint.memory import InMemorySaver

    from misslens.agents.grader.coach import coach_ask, coach_history, coach_thread

    cp, tid = InMemorySaver(), coach_thread("ccar-f", 1)
    llm = FakeRoleLLM([call("search_material", {"query": "tool_choice"}, 1),
                       call("learner_history", {"task_id": "2.3"}, 2),
                       "첫 해설입니다.",
                       call("search_material", {"query": "tool_choice auto vs any"}, 3),   # tools again next turn
                       "auto는 모델이 고르고 any는 반드시 하나를 부릅니다. (출처: lesson/tool-choice)"])
    first = coach_explain(llm, PACK, row(), ledger=ledger, index=index, checkpointer=cp, thread_id=tid)
    assert first.text == "첫 해설입니다." and len(first.tool_calls) == 2
    second = coach_ask(llm, PACK, "auto와 any는 어떻게 달라요?", ledger=ledger, index=index,
                       checkpointer=cp, thread_id=tid)
    assert second.text.startswith("auto는") and second.tool_calls == ["search_material(query='tool_choice auto vs any')"]
    assert second.sources[0] == "lesson/tool-choice"
    # the follow-up saw the whole conversation
    assert any(getattr(m, "content", "") == "첫 해설입니다." for m in llm.prompts[-1])
    turns = coach_history(cp, tid)
    assert [t.question for t in turns] == [None, "auto와 any는 어떻게 달라요?"]
    assert turns[1].sources[0] == "lesson/tool-choice"


def test_follow_up_failure_is_a_notice(ledger, index):
    from langgraph.checkpoint.memory import InMemorySaver

    from misslens.agents.grader.coach import coach_ask, coach_history

    cp = InMemorySaver()
    llm = FakeRoleLLM(["첫 해설", LLMCallError("explainer", [])])
    coach_explain(llm, PACK, row(), ledger=ledger, index=index, checkpointer=cp, thread_id="t")
    res = coach_ask(llm, PACK, "질문", ledger=ledger, index=index, checkpointer=cp, thread_id="t")
    assert "답하지 못했습니다" in res.text
    assert len(coach_history(cp, "t")) == 1                    # the failed turn is not shown as answered


def test_source_label_keeps_urls_and_adds_pdf_pages():
    from misslens.agents.grader.coach import source_label

    assert source_label("https://x.example/course/1", "[page 3]") == "https://x.example/course/1"
    assert source_label("guide.pdf", "...\n[page 13]\nTool use...") == "guide.pdf p.13"
    assert source_label("notes.md", "no page") == "notes.md"


def test_search_tool_knows_and_filters_skilljar_lessons(ledger, index):
    """D49: the model must know collected lessons are indexed, and be able to search only them."""
    index.add([IndexedChunk("Permission modes: plan mode reads only; auto accepts edits.",
                            "https://anthropic-partners.skilljar.com/claude-code-in-action/486932", "L1", [])])
    tools = {t.name: t for t in make_tools(PACK, ledger, index)}
    desc = tools["search_material"].description
    assert "skilljar course 'claude-code-in-action' (1 page(s))" in desc and "source" in desc
    out = tools["search_material"].invoke({"query": "permission modes", "source": "skilljar"})
    assert "claude-code-in-action/486932" in out and "lesson/caching" not in out
    assert "No matching material in guide" in tools["search_material"].invoke({"query": "x", "source": "guide"})


def test_conversation_can_start_with_a_question_on_a_miss_or_a_right_answer(ledger, index):
    """①② : no explanation needed first; a right answer is not treated as a miss."""
    from dataclasses import replace

    from langgraph.checkpoint.memory import InMemorySaver

    from misslens.agents.grader.coach import SITUATION_RIGHT, coach_ask, coach_history

    cp = InMemorySaver()
    llm = FakeRoleLLM(["답 1", "답 2", "맞힌 문제 답"])
    coach_ask(llm, PACK, "왜 이게 정답이죠?", ledger=ledger, index=index, checkpointer=cp, thread_id="m", row=row())
    system, first = llm.prompts[0][0], llm.prompts[0][1]
    assert "READING miss" in system.content and "왜 이게 정답이죠?" in first.content and "Question:" in first.content
    coach_ask(llm, PACK, "다른 보기는요?", ledger=ledger, index=index, checkpointer=cp, thread_id="m", row=row())
    assert len([m for m in llm.prompts[1] if m.type == "system"]) == 1          # context sent once
    assert [t.question for t in coach_history(cp, "m")] == ["왜 이게 정답이죠?", "다른 보기는요?"]

    right = replace(row(), is_correct=True, miss_type=None, missed_phrase=None, chosen_index=0)
    coach_ask(llm, PACK, "B는 왜 틀려요?", ledger=ledger, index=index, checkpointer=cp, thread_id="r", row=right)
    right_system = llm.prompts[2][0].content
    assert SITUATION_RIGHT[:40] in right_system and "Diagnosis:" not in right_system and "answered a practice question wrong" not in right_system
    assert coach_history(cp, "r")[0].question == "B는 왜 틀려요?"
