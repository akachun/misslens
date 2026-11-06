"""Supervisor 라우팅(SERVICE F10, ARCHITECTURE §2, D6/D20).

명령과 버튼은 intent를 달고 들어오므로 바로 분기로 간다(모델 호출 없음). 자유 문장은 router 모델(Haiku 4.5)에
보내 {intent, domain, count, confidence}를 받는다. confidence가 낮거나 intent를 알 수 없으면 안내 메시지로
끝내고 아무것도 실행하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from misslens.exam_pack import ExamPack
from misslens.llm import LLMCallError

logger = logging.getLogger(__name__)

INTENTS = ("plan_quiz", "quiz", "status", "collect", "ingest", "submit", "resume")   # resume: 버튼 전용
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
or for a plan is "plan_quiz". "status" is only the overall progress report.
A question that asks to explain a concept, feature, setting or exam strategy, or asks about the learner's record on
one task statement (an id like "2.3"), is "unknown": the coach answers it, even when it names an exam area.
A vague remark with no clear request (for example "I need to study") is "unknown" with low confidence.
Fill domain only when the learner clearly names an area. Be honest about confidence."""


def classify(pack: ExamPack, text: str, llm: Any) -> RouteDecision | None:
    """router 모델의 분류 결과 그대로. 호출이나 파싱이 실패하면 None이다(질의 API도 같은 분류를 쓴다)."""
    domains = "\n".join(f"- {d.id}: {d.name} ({d.name_native or ''})" for d in pack.domains)
    try:
        out = llm.with_structured_output(RouteDecision).invoke(
            [SystemMessage(ROUTER_SYSTEM.format(domains=domains)), HumanMessage(text)])
        out = RouteDecision.model_validate(out) if isinstance(out, dict) else out
        if out is None:
            raise ValueError("no structured output")
        return out
    except (LLMCallError, ValidationError, ValueError) as exc:
        logger.warning("router failed: %s", exc)
        return None


def route_args(pack: ExamPack, out: RouteDecision) -> dict:
    args: dict = {}
    if out.count:
        args["count"] = max(1, min(int(out.count), 30))
    if out.domain and out.domain in {d.id for d in pack.domains}:
        args["domain"] = out.domain
    return args


def route_text(pack: ExamPack, text: str, llm: Any) -> tuple[str, dict, str | None]:
    """-> (intent, args, message). intent "end"는 메시지를 보여 주고 아무것도 실행하지 않는다는 뜻이다."""
    out = classify(pack, text, llm)
    if out is None:
        return "end", {}, HELP
    if out.intent == "unknown" or out.confidence < MIN_CONFIDENCE:
        return "end", {}, HELP
    if out.intent == "ingest":
        return "end", {}, "파일 자료는 `ingest <파일 또는 폴더>`로 넣어 주세요(경로는 문장에서 받지 않습니다)."
    return out.intent, route_args(pack, out), None


__all__ = ["HELP", "INTENTS", "MIN_CONFIDENCE", "RouteDecision", "classify", "route_args", "route_text"]
