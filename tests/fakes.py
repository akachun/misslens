"""Fake LLMs for tests (CLAUDE.md: replace LLMs with GenericFakeChatModel)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda


class FakeRoleLLM:
    """Stands in for misslens.llm.RoleLLM.

    Each queued response is a dict / pydantic model (returned as JSON by a GenericFakeChatModel and parsed
    into the requested schema), a plain string (for .invoke), or an Exception (raised on that call).
    """

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.prompts: list[Any] = []

    def _pop(self):
        if not self.responses:
            raise AssertionError("FakeRoleLLM ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _model(self, item) -> GenericFakeChatModel:
        if hasattr(item, "model_dump_json"):
            text = item.model_dump_json()
        elif isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        return GenericFakeChatModel(messages=iter([AIMessage(content=text)]))

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self._model(self._pop()).invoke(prompt)

    def with_structured_output(self, schema, **kwargs):
        def run(prompt):
            self.prompts.append(prompt)
            msg = self._model(self._pop()).invoke(prompt)
            return schema.model_validate_json(msg.content)

        return RunnableLambda(run)


def draft(answer_index: int = 0, *, stem_extra: str = "", kcs=("must not store passwords", "within 12 hours"),
          bad_violates: str | None = None) -> dict:
    """A QuestionDraft-shaped dict that passes the validator unless told otherwise."""
    stem = (f"A team builds an agent that must not store passwords and must answer within 12 hours. {stem_extra}"
            "What should they do?")
    violates = [kcs[0], kcs[1], kcs[0], kcs[1]]
    violates[answer_index] = None
    if bad_violates is not None:
        violates[(answer_index + 1) % 4] = bad_violates
    return {
        "stem_en": stem,
        "stem_native": "에이전트는 비밀번호를 저장하면 안 되고 12시간 안에 답해야 한다. 무엇을 해야 하나?",
        "options": [{"text_en": f"Option {i}", "text_native": f"보기 {i}", "violates": v}
                    for i, v in enumerate(violates)],
        "answer_index": answer_index,
        "key_constraints": list(kcs),
        "explanation_native": "정답은 두 조건을 모두 지킨다.",
    }


PASS = {"answer_supported": True, "violations_real": True, "single_answer": True, "details_accurate": True,
        "reason": "ok"}
FAIL = {"answer_supported": False, "violations_real": True, "single_answer": True, "details_accurate": True,
        "reason": "answer not supported"}
