"""실제 Bedrock: 문제 하나를 전체 파이프라인(Sonnet 초안 + validator + Haiku 검사)으로 만든다. 할당량을 쓴다.

근거 검증에는 실제 참고 자료가 필요하다. 세부 항목 문장만으로는 검증기가 진짜 플래그와 지어낸 플래그를
구별하지 못한다(2026-11-04에 `-p`와 지어낸 플래그를 모두 거부했다). 그래서 이 테스트는 Phase 3 검색이 할 것처럼
짧은 문서 발췌를 준다.
"""

import random

import pytest

from misslens.agents.generator import Chunk, Generator
from misslens.agents.generator.schema import validation_errors
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger

pytestmark = pytest.mark.live

DOC = Chunk(
    "Claude Code CLI reference. `claude -p \"query\"` (also `--print`) runs a single query non-interactively: it "
    "prints the response and exits, which is how Claude Code runs in scripts and CI/CD pipelines. "
    "`--output-format` accepts `text` (default), `json` or `stream-json`; use `json` when a later pipeline step "
    "must parse the result. Without -p, `claude` starts an interactive session that waits for user input. "
    "`--max-turns` limits agentic turns in non-interactive mode. `--allowedTools` lists tools that run without "
    "a permission prompt.",
    "docs: Claude Code CLI reference (excerpt)")


class StaticContext:
    grounding = "retrieved"

    def __call__(self, pack, task_id):
        return [DOC]


def test_generate_one_real_question():
    pack = load_exam_pack("ccar-f")
    ledger = Ledger(":memory:")
    res = Generator(pack, ledger, context=StaticContext(), rng=random.Random(0)).generate_for_task("3.6")
    print("tries:", res.tries, "reasons:", res.reasons)
    assert res.question is not None, res.reasons
    q = res.question
    print(q.model_dump_json(indent=2))
    assert validation_errors(q) == []
    assert any("가" <= ch <= "힣" for ch in q.stem_native)   # 한국어 번역이 있다
    ledger.close()
