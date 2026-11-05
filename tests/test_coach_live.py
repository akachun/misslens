"""실제 Bedrock(Sonnet explainer): 코치가 Converse toolUse/toolResult로 실제로 도구를 호출한다. 할당량을 쓴다."""

import pytest

from misslens.agents.grader.coach import coach_explain
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger
from misslens.llm import get_llm
from misslens.retrieval import ChunkIndex, IndexedChunk
from test_coach import row
from test_retrieval import FakeEmbedder

pytestmark = pytest.mark.live


def test_coach_uses_tools_with_bedrock(tmp_path):
    pack = load_exam_pack("ccar-f")
    idx = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    idx.add([IndexedChunk("tool_choice can be auto, any, or a specific tool: {\"type\": \"tool\", \"name\": ...} "
                          "forces the model to call that tool. Distribute 4-5 tools per subagent, scoped to its role.",
                          "lesson/tool-use", "h1", ["2.3"])])
    ledger = Ledger(":memory:")
    res = coach_explain(get_llm("explainer"), pack, row(), ledger=ledger, index=idx)
    ledger.close()
    print(res.tool_calls, res.sources, res.text)
    assert res.used_tools, "coach answered without calling any tool"
    assert any("가" <= ch <= "힣" for ch in res.text)
