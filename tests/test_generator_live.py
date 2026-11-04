"""Real Bedrock: one question through the full pipeline (Sonnet draft + validator + Haiku check). Uses quota.

The grounding check needs real reference material: with the task statement alone, the verifier cannot tell
real flags from invented ones (it rejected both `-p` and a made-up flag on 2026-11-04). So this test gives a
small docs excerpt, the way Phase 3 retrieval will.
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
    assert any("가" <= ch <= "힣" for ch in q.stem_native)   # Korean translation present
    ledger.close()
