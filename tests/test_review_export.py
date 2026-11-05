"""D58: 상위 모델 검토용 문제 내보내기(모델 호출 없음, 제출한 세트만, 근거 부록)."""

from __future__ import annotations

import pytest

from fakes import draft
from misslens.agents.generator.context import Chunk
from misslens.cli import main
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Answer, Ledger
from misslens.review_export import REVIEW_PROMPT, build_review_markdown, collect_questions

PACK = load_exam_pack("ccar-f")


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def add_set(ledger, n=2, *, submit=True, task_id="2.3"):
    sid = ledger.create_set(PACK.exam_id, n)
    qids = [ledger.add_question(sid, seq=i, task_id=task_id, domain="d2", blueprint_version=PACK.version,
                                payload={**draft(0), "task_id": task_id, "domain": "d2", "scenario_id": None,
                                         "grounding": "retrieved"}) for i in range(n)]
    ledger.start_set(sid)
    if submit:
        ledger.submit_set(sid, [Answer(q, 0, 100) for q in qids])
    return sid, qids


def test_only_submitted_sets_are_exported(ledger):
    done, qids = add_set(ledger)
    add_set(ledger, submit=False)                     # 풀고 있는 세트: 정답이 드러나므로 빼야 한다
    qs = collect_questions(ledger, PACK.exam_id)
    assert [q["id"] for q in qs] == qids
    assert collect_questions(ledger, PACK.exam_id, [999]) == []
    assert [q["set_id"] for q in collect_questions(ledger, PACK.exam_id, [done])] == [done, done]


def test_markdown_has_prompt_answer_violates_and_evidence(ledger):
    add_set(ledger)
    calls = []

    def context(pack, task_id):
        calls.append(task_id)
        return [Chunk("## Lesson heading\nUse ```code``` blocks.", "https://example.test/lesson")]

    out = build_review_markdown(PACK, ledger, context=context)
    md = out.markdown
    assert md.startswith(REVIEW_PROMPT.format(exam_name=PACK.name))
    assert out.questions == 2 and out.tasks == ["2.3"]
    assert calls == ["2.3"]                           # 세부 항목마다 한 번만 검색한다
    assert "✅ 정답" in md and 'violates: "must not store passwords"' in md
    assert md.count("## Q") == 2
    # 자료 속 제목·백틱은 더 긴 울타리의 코드 블록 안에 있어 문서 구조와 섞이지 않는다
    assert "````text\n## Lesson heading" in md and "https://example.test/lesson" in md


def test_evidence_modes(ledger):
    add_set(ledger)
    ctx = lambda pack, tid: [Chunk("secret course text", "lesson-url")]  # noqa: E731
    sources = build_review_markdown(PACK, ledger, evidence="sources", context=ctx).markdown
    assert "- lesson-url" in sources and "secret course text" not in sources
    none = build_review_markdown(PACK, ledger, evidence="none", context=ctx).markdown
    assert "lesson-url" not in none and "--evidence none" in none
    with pytest.raises(ValueError):
        build_review_markdown(PACK, ledger, evidence="all")


def test_cli_writes_file_without_index(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    lg = Ledger(path)
    add_set(lg)
    lg.close()
    monkeypatch.setattr("misslens.cli.Ledger", lambda: Ledger(path))
    monkeypatch.setattr("misslens.cli.open_index", lambda exam_id, **kw: None)
    out = tmp_path / "review.md"
    assert main(["export-questions", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "총 2문제" in text and "자료 색인이 없어" in text
    empty = tmp_path / "empty.sqlite"
    Ledger(empty).close()
    monkeypatch.setattr("misslens.cli.Ledger", lambda: Ledger(empty))
    assert main(["export-questions", "-o", str(tmp_path / "none.md")]) == 1
