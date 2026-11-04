import random

import pytest

from fakes import FakeRoleLLM, draft
from misslens.agents.generator import QuestionDraft, validate_question
from misslens.agents.grader import (
    build_reask_form,
    decoy_count,
    diagnosis_rows,
    explain_miss,
    highlight,
    submit_review,
    to_reask_answers,
)
from misslens.exam_pack import ExamPack, load_exam_pack
from misslens.ledger import Answer, Ledger, miss_breakdown, top_missed_phrases

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def question(answer_index=0):
    return validate_question(QuestionDraft.model_validate(draft(answer_index)), task_id="1.1", domain="d1",
                             scenario_id=None, grounding="g").model_dump()


def solved_set(ledger, choices):
    """One question per choice, answer always 0. Returns (set_id, attempt ids)."""
    sid = ledger.create_set(PACK.exam_id, len(choices))
    qids = [ledger.add_question(sid, seq=i, task_id="1.1", domain="d1", blueprint_version=2, payload=question(0))
            for i in range(len(choices))]
    ledger.start_set(sid)
    return sid, ledger.submit_set(sid, [Answer(q, c, 1000) for q, c in zip(qids, choices)])


@pytest.mark.parametrize("n_correct, expected", [(0, 0), (1, 1), (2, 2), (5, 2), (10, 3), (20, 6)])
def test_decoy_count(n_correct, expected):
    assert decoy_count(n_correct) == expected


def test_form_has_all_misses_plus_decoys_shuffled(ledger):
    sid, a = solved_set(ledger, [1, 2, 0, 0, 0, 0, 0, 0, 0, 0])   # 2 misses, 8 correct
    items = build_reask_form(ledger.set_questions(sid), ledger.set_attempts(sid), rng=random.Random(3))
    misses = {i.attempt_id for i in items if not i.is_decoy}
    assert misses == {a[0], a[1]}
    assert sum(i.is_decoy for i in items) == decoy_count(8)
    for it in items:
        assert sorted(it.order) == [0, 1, 2, 3]
        assert it.stem.startswith("에이전트")                      # learner language
        assert it.options == [f"보기 {k}" for k in it.order]       # displayed via the mapping


def test_mapping_back_to_original_order(ledger):
    sid, a = solved_set(ledger, [1, 0, 0])
    items = build_reask_form(ledger.set_questions(sid), ledger.set_attempts(sid), rng=random.Random(0))
    # Learner picks the displayed position of the ORIGINAL answer (index 0) everywhere, confident.
    responses = [(it.order.index(0), True) for it in items]
    answers = to_reask_answers(items, responses)
    assert all(x.reask_choice == 0 for x in answers)


def test_review_end_to_end_records_reading_phrase_and_baseline(ledger):
    sid, a = solved_set(ledger, [1, 2, 0, 0, 0])
    items = build_reask_form(ledger.set_questions(sid), ledger.set_attempts(sid), rng=random.Random(1))
    responses = []
    for it in items:
        if it.attempt_id == a[0]:
            responses.append((it.order.index(0), True))      # miss -> right + sure = reading
        elif it.attempt_id == a[1]:
            responses.append((it.order.index(3), True))      # miss -> wrong again = concept
        else:
            responses.append((it.order.index(0), True))      # decoy right
    submit_review(ledger, PACK, sid, items, responses)
    rows = {r.attempt_id: r for r in diagnosis_rows(ledger, sid)}
    assert rows[a[0]].miss_type == "reading"
    assert rows[a[0]].missed_phrase == question(0)["options"][1]["violates"]
    assert rows[a[1]].miss_type == "concept" and rows[a[1]].missed_phrase is None
    assert miss_breakdown(ledger, PACK.exam_id, set_id=sid).baseline == 1.0
    assert top_missed_phrases(ledger, PACK.exam_id)[0][1] == 1


def test_same_language_exam_skips_reask(ledger):
    same = ExamPack.model_validate({**PACK.model_dump(), "learner_language": "en"})
    sid, a = solved_set(ledger, [1, 0, 0])
    submit_review(ledger, same, sid, [], [])
    rows = diagnosis_rows(ledger, sid)
    assert [r.miss_type for r in rows] == ["concept", None, None]
    assert not any(r.is_decoy for r in rows)


def test_invalid_miss_left_out_of_form_and_review(ledger):
    sid, a = solved_set(ledger, [1, 2, 0, 0])
    ledger.mark_invalid(a[1])
    items = build_reask_form(ledger.set_questions(sid), ledger.set_attempts(sid), rng=random.Random(0))
    assert a[1] not in {i.attempt_id for i in items}
    submit_review(ledger, PACK, sid, items, [(it.order.index(0), True) for it in items])
    assert ledger.get_set(sid)["status"] == "reviewed"


def test_highlight_marks_phrase_case_insensitive():
    out = highlight("Do it WITHIN 12  hours, please.", "within 12 hours", "<", ">")
    assert out == "Do it <WITHIN 12  hours>, please."
    assert highlight("abc", None) == "abc"


def test_explain_miss_uses_llm_and_falls_back(ledger):
    sid, a = solved_set(ledger, [1])
    row = diagnosis_rows(ledger, sid)[0]
    llm = FakeRoleLLM(["고른 보기는 비밀번호 저장 금지 조건을 어긴다."])
    assert explain_miss(llm, PACK, row).startswith("고른 보기")
    system = llm.prompts[0][0].content
    assert "Korean" in system
    broken = FakeRoleLLM([RuntimeError("bedrock down")])
    assert explain_miss(broken, PACK, row) == row.question["explanation_native"]
