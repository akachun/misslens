"""모델에 보내는 글은 영어로 남아 있어야 한다(D57).

주석과 docstring은 한국어로 쓰지만, 아래 docstring은 모델 입력의 일부다. 구조화 출력용 Pydantic 클래스의
docstring은 JSON 스키마 설명으로, `StructuredTool.from_function`에 설명 없이 넘긴 함수의 docstring은
도구 설명으로 모델에 전달된다. 한국어로 바뀌면 화면에는 티가 나지 않고 출제·진단 입력만 조용히 바뀐다.
"""

from __future__ import annotations

import re

from misslens.agents.generator.schema import QuestionDraft
from misslens.agents.grader.coach import make_tools
from misslens.agents.planner import PlanDraft
from misslens.exam_pack import load_exam_pack

HANGUL = re.compile(r"[가-힣]")


def test_structured_output_schemas_are_english():
    for model in (QuestionDraft, PlanDraft):
        schema = model.model_json_schema()
        assert schema.get("description"), model.__name__
        assert not HANGUL.search(str(schema)), model.__name__


def test_coach_tool_descriptions_are_english():
    tools = make_tools(load_exam_pack("ccar-f"), ledger=None, index=None)
    assert {t.name for t in tools} == {"search_material", "learner_history"}
    for t in tools:
        assert t.description and not HANGUL.search(t.description), t.name
