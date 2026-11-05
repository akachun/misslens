"""Practice bank: parsing, quality flags, per-task style samples, copy guard (TASK T6-b, D39)."""

from __future__ import annotations

import random

import pytest

from fakes import PASS, FakeRoleLLM, draft
from misslens.agents.generator import Generator
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger
from misslens.practice import (
    PracticeFormatError, PracticeSamples, load_bank, parse_dir, parse_set, save_bank, summarize,
)

PACK = load_exam_pack("ccar-f")


def q_md(n, task, stem, opts=("one", "two", "three", "four")):
    lines = [f'<a id="question-{n}"></a>', f"## {n}. {stem}", "", f"Subdomain {task}: Some title", ""]
    lines += [f"- **{c}.** {o}" for c, o in zip("ABCD", opts)]
    return "\n".join(lines) + "\n\n---\n"


def a_md(n, letter, exps, site=True):
    head = f"## {n}번 — {'사이트 제시 ' if site else ''}정답 {letter}"
    return "\n".join([head, ""] + [f"**{c}.** {e}" for c, e in zip("ABCD", exps)]) + "\n\n---\n"


OK = ["Incorrect. no", "Correct. yes", "Incorrect. no", "Wrong here"]


def write_set(d, no, questions, answers):
    (d / f"CCAR-F-set{no}-questions.md").write_text(
        "# t\n\n출처: [Site](https://example.com/quiz), 2026\n\n" + "".join(questions), encoding="utf-8")
    (d / f"CCAR-F-set{no}-answers.md").write_text("# a\n\n" + "".join(answers), encoding="utf-8")


@pytest.fixture
def src(tmp_path):
    write_set(tmp_path, 1, [q_md(1, "2.3", "Agents share tools. Which tool_choice setting forces one tool?"),
                            q_md(2, "2.3", "A subagent has 18 tools and picks wrong ones. What helps most?")],
              [a_md(1, "B", OK, site=False), a_md(2, "C", ["x", "y", "z", "w"])])
    write_set(tmp_path, 2, [q_md(1, "4.4", "Agents share tools. Which TOOL_CHOICE setting  forces one tool?"),
                            q_md(2, "4.4", "Validation retries keep failing on the same field. Why?")],
              [a_md(1, "B", OK), a_md(2, "A", ["Correct. a", "Incorrect", "Incorrect", "Incorrect"])])
    (tmp_path / "CCAR-F-index.md").write_text("주의: 2세트 2번의 설명은 공식 문서로 확인해야 합니다.", encoding="utf-8")
    return tmp_path


def test_parse_dir_flags_duplicates_unconfirmed_and_disputed(src):
    bank = {q.id: q for q in parse_dir(src, valid_task_ids=set(PACK.task_ids))}
    assert list(bank) == ["s1q01", "s1q02", "s2q01", "s2q02"]
    q = bank["s1q01"]
    assert q.task_id == "2.3" and q.answer_index == 1 and q.options[3] == "four" and q.answer_confirmed
    assert q.source == "https://example.com/quiz"
    assert not bank["s1q02"].answer_confirmed          # no explanation says "Correct"
    assert bank["s2q01"].duplicate_of == "s1q01"       # same stem up to case and spaces
    assert bank["s2q02"].disputed                      # named in the index caveat
    assert [q.id for q in bank.values() if q.usable_as_sample] == ["s1q01"]


def test_parse_rejects_broken_files(src):
    with pytest.raises(PracticeFormatError):
        parse_set(q_md(1, "1.1", "stem", opts=("a", "b", "c")), a_md(1, "A", OK), 1)
    with pytest.raises(PracticeFormatError):
        parse_set(q_md(1, "1.1", "stem"), "", 1)
    with pytest.raises(PracticeFormatError, match="not in the exam pack"):
        write_set(src, 3, [q_md(1, "9.9", "stem")], [a_md(1, "A", OK)])
        parse_dir(src, valid_task_ids=set(PACK.task_ids))


def test_save_load_and_summary(src, tmp_path):
    bank = parse_dir(src)
    save_bank(bank, "ccar-f", root=tmp_path / "bank")
    again = load_bank("ccar-f", root=tmp_path / "bank")
    assert [q.id for q in again] == [q.id for q in bank] and again[2].duplicate_of == "s1q01"
    s = summarize(again)
    assert s["total"] == 4 and s["usable"] == 1 and s["usable_by_task"] == {"2.3": 1}
    assert load_bank("none", root=tmp_path / "bank") == []


def test_samples_are_per_task_and_only_usable(src):
    samples = PracticeSamples(parse_dir(src), per_task=2, rng=random.Random(0))
    out = samples("2.3")
    assert len(out) == 1 and "tool_choice" in out[0] and out[0].endswith("Answer: B")
    assert samples("4.4") == [] and samples("1.1") == []


def test_copy_guard(src):
    samples = PracticeSamples(parse_dir(src))
    assert samples.copy_reason("2.3", "Agents share tools. Which tool_choice setting forces one tool now?")
    assert samples.copy_reason("2.3", "A billing agent must refund within 12 hours without storing card data.") is None
    assert samples.copy_reason("1.1", "Agents share tools. Which tool_choice setting forces one tool?") is None


def test_generator_uses_task_samples_and_regenerates_copies(src):
    samples = PracticeSamples(parse_dir(src), rng=random.Random(0))
    copied = draft(0, stem_extra="Agents share tools. Which tool_choice setting forces one tool? ")
    copied["stem_en"] = "Agents share tools. Which tool_choice setting forces one tool? " + copied["stem_en"]
    gen_llm = FakeRoleLLM([copied, draft(1)])
    lg = Ledger(":memory:")
    try:
        g = Generator(PACK, lg, gen_llm=gen_llm, verify_llm=FakeRoleLLM([PASS]), samples=samples,
                      copy_guard=lambda t, stem: samples.copy_reason("2.3", stem), rng=random.Random(0))
        r = g.generate_for_task("2.3")
    finally:
        lg.close()
    assert r.question is not None and r.tries == 2
    assert "too close to an existing practice question" in r.reasons[0]
    first_prompt = gen_llm.prompts[0][1].content
    assert "<style_examples>" in first_prompt and "NOT a source of facts" in first_prompt
    assert "Answer: B" in first_prompt
