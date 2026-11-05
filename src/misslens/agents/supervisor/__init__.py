"""Supervisor routing (SERVICE F10, ARCHITECTURE §2, D6/D20).

Commands and buttons arrive with an intent and go straight to a branch (no model call). Free text goes to the
router model (Haiku 4.5) for {intent, domain, count, confidence}; low confidence or an unknown intent ends with a
guide message and runs nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from misslens.exam_pack import ExamPack
from misslens.llm import LLMCallError

logger = logging.getLogger(__name__)

INTENTS = ("plan_quiz", "quiz", "status", "collect", "ingest", "submit", "resume")   # resume: buttons only
MIN_CONFIDENCE = 0.6
DEFAULT_COUNT = 10

HELP = ("무엇을 할지 확실하지 않아 실행하지 않았습니다. 예: \"오늘 공부 계획 세워줘\", \"도구 설계 영역 5문제\", "
        "\"현황 보여줘\", \"교안 수집해줘\". 파일을 넣을 때는 `ingest <경로>`를 쓰세요.")


class RouteDecision(BaseModel):
    intent: Literal["plan_quiz", "quiz", "status", "collect", "ingest", "unknown"] = Field(
        description="plan_quiz: make today's plan then a set; quiz: a set right away (optionally one domain); "
                    "status: show progress; collect: collect course pages from the login site; "
                    "ingest: add files; unknown: anything else")
    domain: str | None = Field(default=None, description="Domain id (e.g. d2) when the learner names one area")
    count: int | None = Field(default=None, description="Number of questions if stated")
    confidence: float = Field(description="0-1, how sure you are about the intent")


ROUTER_SYSTEM = """You route a study coach's request. The learner writes in Korean or English.
Exam domains (id: name):
{domains}
Pick one intent. A request to study, practice or solve without asking for a plan is "quiz"; asking what to study
or for a plan is "plan_quiz". Fill domain only when the learner clearly names an area. Be honest about confidence."""


def route_text(pack: ExamPack, text: str, llm: Any) -> tuple[str, dict, str | None]:
    """-> (intent, args, message). intent "end" means: show the message and run nothing."""
    domains = "\n".join(f"- {d.id}: {d.name} ({d.name_native or ''})" for d in pack.domains)
    try:
        out = llm.with_structured_output(RouteDecision).invoke(
            [SystemMessage(ROUTER_SYSTEM.format(domains=domains)), HumanMessage(text)])
        out = RouteDecision.model_validate(out) if isinstance(out, dict) else out
        if out is None:
            raise ValueError("no structured output")
    except (LLMCallError, ValidationError, ValueError) as exc:
        logger.warning("router failed: %s", exc)
        return "end", {}, HELP
    if out.intent == "unknown" or out.confidence < MIN_CONFIDENCE:
        return "end", {}, HELP
    if out.intent == "ingest":
        return "end", {}, "파일 자료는 `ingest <파일 또는 폴더>`로 넣어 주세요(경로는 문장에서 받지 않습니다)."
    args: dict = {}
    if out.count:
        args["count"] = max(1, min(int(out.count), 30))
    if out.domain and out.domain in {d.id for d in pack.domains}:
        args["domain"] = out.domain
    return out.intent, args, None


__all__ = ["HELP", "INTENTS", "RouteDecision", "route_text"]
