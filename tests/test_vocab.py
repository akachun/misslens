"""D54: word lists from reviewed sets (verbatim expressions only), cumulative word book, known marks."""

from __future__ import annotations

import pytest

from fakes import FakeRoleLLM, draft
from misslens.agents.grader.vocab import VocabItem, clean_items, context_sentence, extract_vocab
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Answer, Ledger

PACK = load_exam_pack("ccar-f")
# draft(0) stem: "A team builds an agent that must not store passwords and must answer within 12 hours. What should they do?"


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def reviewed_set(ledger, wrong=(True, False)):
    sid = ledger.create_set(PACK.exam_id, len(wrong))
    qids = [ledger.add_question(sid, seq=i, task_id="2.3", domain="d2", blueprint_version=PACK.version,
                                payload={**draft(0), "task_id": "2.3", "domain": "d2", "scenario_id": None,
                                         "grounding": "retrieved"}) for i in range(len(wrong))]
    ledger.start_set(sid)
    ledger.submit_set(sid, [Answer(q, 1 if w else 0, 100) for q, w in zip(qids, wrong)])
    return sid, qids


def item(q, term, kind="condition", meaning="뜻", role="역할"):
    return VocabItem(q=q, term=term, kind=kind, meaning=meaning, role=role)


def test_clean_items_keeps_verbatim_unique_missed_first(ledger):
    sid, qids = reviewed_set(ledger)
    qs = ledger.set_questions(sid)
    atts = {a["question_id"]: a for a in ledger.set_attempts(sid)}
    out = clean_items([item(2, "within 12 hours"),                  # right-answer question
                       item(1, "must not store passwords"),         # missed question: goes first
                       item(1, "MUST NOT store  passwords"),        # same term, different case/spacing: dropped
                       item(1, "never retain secrets"),             # not in the text: dropped
                       item(1, "", meaning="x"),                    # empty: dropped
                       item(9, "within 12 hours"),                  # no such question: dropped
                       item(1, "Option 2", kind="general")], qs, atts)
    assert [(o["term"], o["from_miss"]) for o in out] == [("must not store passwords", True), ("Option 2", True),
                                                          ("within 12 hours", False)]
    assert out[0]["question_id"] == qids[0]


def test_extract_stores_once_and_reads_back_without_the_model(ledger):
    sid, _ = reviewed_set(ledger)
    llm = FakeRoleLLM([{"items": [item(1, "must not store passwords").model_dump(),
                                  item(1, "agent", kind="technical", meaning="에이전트").model_dump()]},
                       {"items": []}])                              # follow-up round for Q2 gives nothing
    first = extract_vocab(llm, PACK, ledger, sid)
    assert [v["term"] for v in first] == ["must not store passwords", "agent"]
    prompt = llm.prompts[0][1].content
    assert "MISSED" in prompt or "KEY:" in prompt                   # missed question flagged for the model
    again = extract_vocab(FakeRoleLLM([]), PACK, ledger, sid)       # an empty fake would raise if called
    assert [v["term"] for v in again] == [v["term"] for v in first]


def test_word_book_counts_across_sets_and_known_marks(ledger):
    s1, _ = reviewed_set(ledger, wrong=(True,))
    s2, _ = reviewed_set(ledger, wrong=(False,))
    for sid, miss in ((s1, True), (s2, False)):
        q = ledger.set_questions(sid)[0]
        ledger.save_vocab(PACK.exam_id, sid, [{"question_id": q["id"], "term": "must not store passwords",
                                               "term_key": "must not store passwords", "kind": "condition",
                                               "meaning": "비밀번호를 저장하면 안 된다", "role": "r", "from_miss": miss}])
    (b,) = ledger.vocab_book(PACK.exam_id)
    assert (b["seen"], b["sets"], b["from_miss"], b["known"]) == (2, 2, True, False)
    assert len(b["uses"]) == 2 and "passwords" in b["uses"][0]["stem_en"]
    ledger.mark_known(PACK.exam_id, "must not store passwords")
    assert ledger.vocab_book(PACK.exam_id)[0]["known"] and ledger.set_vocab(s1)[0]["known"]
    ledger.mark_known(PACK.exam_id, "must not store passwords", False)
    assert not ledger.vocab_book(PACK.exam_id)[0]["known"]


def test_context_sentence_finds_the_sentence():
    stem = "A team builds an agent. It must not store passwords and must answer within 12 hours. What should they do?"
    assert context_sentence(stem, "Within 12 hours") == "It must not store passwords and must answer within 12 hours."
    assert context_sentence(stem, "nowhere") is None


def test_questions_skipped_by_the_model_are_asked_again(ledger):
    sid, _ = reviewed_set(ledger, wrong=(True, False))
    llm = FakeRoleLLM([{"items": [item(1, "must not store passwords").model_dump()]},     # stops after Q1
                       {"items": [item(2, "within 12 hours").model_dump()]}])
    got = extract_vocab(llm, PACK, ledger, sid)
    assert sorted(v["term"] for v in got) == ["must not store passwords", "within 12 hours"]
    second = llm.prompts[1]
    assert "(Q2)" in second[0].content and "Q1 (" not in second[1].content and "Q2 (" in second[1].content


def test_long_clauses_only_when_missed(ledger):
    sid, _ = reviewed_set(ledger, wrong=(True, False))
    qs = ledger.set_questions(sid)
    atts = {a["question_id"]: a for a in ledger.set_attempts(sid)}
    long = "A team builds an agent that must not store passwords"     # 10 words, verbatim in the stem
    out = clean_items([item(1, long), item(2, long), item(2, "must not store passwords")], qs, atts)
    assert [o["term"] for o in out] == ["must not store passwords"]     # long clause dropped in both questions
    kc = "must not store passwords"                                     # key constraints are short here; a long
    assert all(len(o["term"].split()) <= 8 or o["term"] == kc for o in out)  # one would pass only for a miss
