"""Real Bedrock smoke tests. Uses quota: run only with `pytest -m live`."""

import pytest
from pydantic import BaseModel

from misslens.llm import get_llm

pytestmark = pytest.mark.live


def test_router_plain_invoke():
    out = get_llm("router").invoke("Reply with the single word: pong")
    assert "pong" in out.content.lower()


class _Pick(BaseModel):
    options: list[str]
    answer_index: int


def test_generator_structured_output():
    out = get_llm("generator").with_structured_output(_Pick).invoke(
        "Give three color names and set answer_index to the color of a clear daytime sky.")
    assert len(out.options) == 3 and out.options[out.answer_index].lower() == "blue"
