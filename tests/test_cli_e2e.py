"""E2E: CLI 5-question set -> submit -> re-ask -> diagnosis, with fake LLMs (TASK Phase 2 done criterion)."""

import io
import random

from rich.console import Console

from fakes import PASS, FakeRoleLLM, draft
from misslens.agents.generator import Generator
from misslens.cli import CLI, LETTERS
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger, miss_breakdown, top_missed_phrases

PACK = load_exam_pack("ccar-f")


class ScriptedLearner:
    """Answers prompts like a learner who misreads question 1, lacks the concept for question 2,
    and knows the rest. Re-ask prompts are answered by looking at the displayed Korean options."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.solve_count = 0
        self.form = None
        self.reask_index = 0
        self.log = []

    def __call__(self, prompt: str) -> str:
        self.log.append(prompt)
        if prompt.startswith("답 (A-D, 건너뛰기"):
            self.solve_count += 1
            if self.solve_count == 1:
                return self._wrong_letter(1)
            if self.solve_count == 2:
                return ""                       # skipped -> wrong
            return self._right_letter(self.solve_count)
        if prompt.startswith("답 (A-D)"):       # re-ask
            item = self.form[self.reask_index]
            self.reask_index += 1
            seq = self.seq_of[item.attempt_id]
            target = self.answer_of[seq] if seq != 2 else (self.answer_of[seq] + 1) % 4
            return LETTERS[item.order.index(target)]
        if prompt.startswith("확신하나요"):
            return "y"
        if prompt.startswith("문제 이상"):
            return ""
        raise AssertionError(f"unexpected prompt {prompt!r}")

    # The learner "sees" the question being shown: read the latest ready question from the ledger.
    def _current(self, n):
        sets = self.ledger._query("SELECT id FROM quiz_sets ORDER BY id DESC LIMIT 1")
        qs = self.ledger.set_questions(sets[0]["id"])
        return qs[n - 1]["payload"]

    def _right_letter(self, n):
        return LETTERS[self._current(n)["answer_index"]]

    def _wrong_letter(self, n):
        return LETTERS[(self._current(n)["answer_index"] + 1) % 4]


def test_cli_five_question_set_records_diagnosis(monkeypatch):
    ledger = Ledger(":memory:")
    gen = Generator(PACK, ledger, gen_llm=FakeRoleLLM([draft(i % 4) for i in range(5)]),
                    verify_llm=FakeRoleLLM([PASS] * 5), rng=random.Random(7))
    learner = ScriptedLearner(ledger)
    out = io.StringIO()
    cli = CLI(PACK, ledger, console=Console(file=out, width=120), ask=learner)

    import misslens.cli as cli_mod
    real_build = cli_mod.build_reask_form

    def capture(questions, attempts, **kw):
        learner.form = real_build(questions, attempts, rng=random.Random(5))
        qid_seq = {q["id"]: q["seq"] + 1 for q in questions}
        learner.seq_of = {a["id"]: qid_seq[a["question_id"]] for a in attempts}
        learner.answer_of = {q["seq"] + 1: q["payload"]["answer_index"] for q in questions}
        return learner.form

    monkeypatch.setattr(cli_mod, "build_reask_form", capture)
    sid = cli.quiz(5, gen, explainer=None)

    attempts = {a["id"]: a for a in ledger.set_attempts(sid)}
    assert len(attempts) == 5
    by_seq = {learner.seq_of[i]: a for i, a in attempts.items()}
    assert by_seq[1]["miss_type"] == "reading" and by_seq[1]["missed_phrase"]
    assert by_seq[2]["miss_type"] == "concept" and by_seq[2]["chosen_index"] is None
    assert sum(a["is_decoy"] for a in attempts.values()) == 2          # 3 correct -> 2 decoys
    assert all(a["elapsed_ms"] is not None for a in attempts.values())
    assert ledger.get_set(sid)["status"] == "reviewed"
    mb = miss_breakdown(ledger, PACK.exam_id, set_id=sid)
    assert (mb.reading, mb.concept, mb.baseline) == (1, 1, 1.0)
    assert top_missed_phrases(ledger, PACK.exam_id)[0][0] == by_seq[1]["missed_phrase"]
    text = out.getvalue()
    assert "제출 완료" in text and "해석 실수" in text and "미끼 기준선" in text
    cli.status()
    assert "범위 " in out.getvalue()
    ledger.close()


def test_cli_mark_invalid_from_review(monkeypatch):
    ledger = Ledger(":memory:")
    gen = Generator(PACK, ledger, gen_llm=FakeRoleLLM([draft(0)]), verify_llm=FakeRoleLLM([PASS]),
                    rng=random.Random(0))
    import misslens.cli as cli_mod
    real_build = cli_mod.build_reask_form
    form = {}

    def capture(questions, attempts, **kw):
        form["items"] = real_build(questions, attempts, rng=random.Random(0))
        return form["items"]

    monkeypatch.setattr(cli_mod, "build_reask_form", capture)

    def answer_index():
        (q,) = ledger.set_questions(ledger._query("SELECT max(id) AS id FROM quiz_sets")[0]["id"])
        return q["payload"]["answer_index"]

    def ask(prompt):
        if prompt.startswith("답 (A-D, 건너뛰기"):
            return LETTERS[(answer_index() + 1) % 4]                       # wrong
        if prompt.startswith("답 (A-D)"):
            return LETTERS[form["items"][0].order.index(answer_index())]   # right on re-ask
        if prompt.startswith("확신하나요"):
            return "y"
        return "1"                                                         # mark question 1 invalid

    cli = CLI(PACK, ledger, console=Console(file=io.StringIO()), ask=ask)
    sid = cli.quiz(1, gen)
    (a,) = ledger.set_attempts(sid)
    assert a["miss_type"] == "reading" and a["missed_phrase"]
    assert a["invalid"] == 1
    assert top_missed_phrases(ledger, PACK.exam_id) == []                 # invalid drops out of the ranking
    ledger.close()
