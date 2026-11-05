"""Real Bedrock (Haiku): Supervisor router and Planner model draft. Uses quota; run with `pytest -m live`."""

import pytest

from misslens.agents.planner import draft_plan
from misslens.agents.supervisor import route_text
from misslens.exam_pack import load_exam_pack
from misslens.ledger import Ledger
from misslens.llm import get_llm

pytestmark = pytest.mark.live
PACK = load_exam_pack("ccar-f")


@pytest.mark.parametrize("text, intent, args", [
    ("오늘 뭐 공부하면 좋을지 계획 세워줘", "plan_quiz", {}),
    ("도구 설계랑 MCP 쪽 5문제만 풀래", "quiz", {"count": 5, "domain": "d2"}),
    ("지금까지 얼마나 했는지 보여줘", "status", {}),
    ("점심 뭐 먹지?", "end", {}),
])
def test_router_classifies_korean_requests(text, intent, args):
    got_intent, got_args, _ = route_text(PACK, text, get_llm("router"))
    assert got_intent == intent
    for k, v in args.items():
        assert got_args.get(k) == v


def test_planner_model_returns_valid_plan():
    ledger = Ledger(":memory:")
    plan, table = draft_plan(PACK, ledger, 10, get_llm("planner"))
    ledger.close()
    assert sum(plan.counts.values()) == 10 and len(table) == 5
    assert plan.source == "model", "model draft fell back to the rule plan"
    assert any("가" <= ch <= "힣" for ch in plan.rationale)
