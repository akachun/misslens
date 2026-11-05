"""Planner 에이전트: ledger 통계로 오늘 계획 -> interrupt① 승인 -> 승인된 계획(SERVICE F4, §4.3).

숫자는 ledger.domain_summary에서만 가져온다(CLAUDE.md 규칙 1). 규칙 기반 초안은 항상 계산한다. planner 모델
(Haiku)은 문제 수를 조정하고 한국어 근거를 쓸 수 있다. 모델이 돌려준 것이 검증을 통과하지 못하면 규칙 기반
문제 수로 돌아가므로, 모델이 실패해도 공부가 막히지 않는다.

Graph:  START -> draft -> approve --interrupt①--> (approved | cancelled) -> END
        학습자가 고친 계획이 유효하지 않으면 approve가 다시 묻는다(interrupt 한 번 더).
Resume values: {"action": "approve", "counts": {"d1": 3, ...}?}  (counts 생략 = 초안대로 승인)
               {"action": "cancel"}
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from misslens.exam_pack import ExamPack
from misslens.ledger import DomainSummary, Ledger, domain_summary
from misslens.llm import LLMCallError

logger = logging.getLogger(__name__)

MAX_SET_SIZE = 30


class PlanError(ValueError):
    pass


class Plan(BaseModel):
    total: int
    counts: dict[str, int]          # 영역 id -> 문제 수
    rationale: str = ""
    source: str = "rule"            # "rule" | "model" | "learner"


class PlanDraft(BaseModel):
    """What the planner model returns."""
    counts: dict[str, int] = Field(description="Domain id -> number of questions; must sum to the requested total")
    rationale_ko: str = Field(description="2-4 Korean sentences: why these domains get these counts today")


def summaries(pack: ExamPack, ledger: Ledger) -> dict[str, DomainSummary]:
    task_domain = {t: pack.domain_of(t).id for t in pack.task_ids}
    return domain_summary(ledger, pack.exam_id, task_domain, task_id_map=pack.task_id_map)


def validate_counts(pack: ExamPack, counts: dict[str, Any], total: int | None = None) -> dict[str, int]:
    known = {d.id for d in pack.domains}
    unknown = sorted(set(counts) - known)
    if unknown:
        raise PlanError(f"알 수 없는 영역: {', '.join(unknown)}")
    try:
        clean = {d.id: int(counts.get(d.id, 0)) for d in pack.domains}
    except (TypeError, ValueError) as exc:
        raise PlanError(f"문제 수는 정수여야 합니다: {exc}") from None
    if any(v < 0 for v in clean.values()):
        raise PlanError("문제 수는 0 이상이어야 합니다")
    s = sum(clean.values())
    if not 1 <= s <= MAX_SET_SIZE:
        raise PlanError(f"전체 문제 수는 1~{MAX_SET_SIZE}개여야 합니다(지금 {s}개)")
    if total is not None and s != total:
        raise PlanError(f"영역별 합계 {s}개가 요청한 {total}개와 다릅니다")
    return clean


def rule_plan(pack: ExamPack, stats: dict[str, DomainSummary], total: int) -> Plan:
    """시험 비중 x (1 + 미출제 세부 항목 비율) x (1.5 - 정답률). 최대 나머지 방식으로 반올림한다."""
    scores = {d.id: d.weight * (1 + stats[d.id].unasked_ratio) * (1.5 - stats[d.id].accuracy) for d in pack.domains}
    z = sum(scores.values())
    raw = {k: total * v / z for k, v in scores.items()}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: total - sum(counts.values())]:
        counts[k] += 1
    weak = sorted(pack.domains, key=lambda d: -scores[d.id])[:2]
    return Plan(total=total, counts=counts, source="rule",
                rationale="미출제 세부 항목이 많고 정답률이 낮은 영역을 시험 비중보다 조금 더 냈습니다: "
                          + ", ".join(f"{d.id} {d.name_native or d.name}" for d in weak) + ".")


def stats_table(pack: ExamPack, stats: dict[str, DomainSummary]) -> list[dict]:
    return [{"domain": d.id, "name": d.name_native or d.name, "weight": d.weight, "tasks": stats[d.id].tasks,
             "asked": stats[d.id].asked, "attempts": stats[d.id].attempts,
             "accuracy": round(stats[d.id].accuracy, 2)} for d in pack.domains]


PLANNER_SYSTEM = """You plan today's practice set for a certification exam learner.
Distribute exactly {total} questions over the domains. Priorities, in order: domains with tasks never asked
(coverage before weakness), then low accuracy, while staying roughly proportional to the exam weights.
Use only the domain ids given. Write the rationale in Korean, 2-4 sentences, citing the numbers."""


def draft_plan(pack: ExamPack, ledger: Ledger, total: int, llm: Any = None) -> tuple[Plan, list[dict]]:
    stats = summaries(pack, ledger)
    base = rule_plan(pack, stats, total)
    table = stats_table(pack, stats)
    if llm is None:
        return base, table
    lines = "\n".join(f"- {r['domain']} {r['name']}: weight {r['weight']:.0%}, tasks {r['tasks']}, asked {r['asked']}, "
                      f"attempts {r['attempts']}, accuracy {r['accuracy']:.0%}" for r in table)
    msgs = [SystemMessage(PLANNER_SYSTEM.format(total=total)),
            HumanMessage(f"Domain statistics:\n{lines}\n\nRule-based suggestion: {base.counts}")]
    try:
        out = llm.with_structured_output(PlanDraft).invoke(msgs)
        out = PlanDraft.model_validate(out) if isinstance(out, dict) else out
        if out is None:
            raise PlanError("no structured output")
        counts = validate_counts(pack, out.counts, total)
        return Plan(total=total, counts=counts, rationale=out.rationale_ko, source="model"), table
    except (LLMCallError, ValidationError, PlanError) as exc:
        logger.warning("planner model draft rejected, using rule plan: %s", exc)
        return base, table


class PlannerState(TypedDict, total=False):
    total: int
    plan: dict | None          # Plan.model_dump()
    plan_table: list[dict]
    plan_status: str | None    # "approved" | "cancelled"
    plan_error: str | None


def build_planner_graph(pack: ExamPack, ledger: Ledger, llm: Any = None, *, checkpointer: Any = None):
    def draft(state: PlannerState) -> PlannerState:
        plan, table = draft_plan(pack, ledger, int(state.get("total") or 10), llm)
        return {"plan": plan.model_dump(), "plan_table": table, "plan_status": None, "plan_error": None}

    def approve(state: PlannerState) -> PlannerState:
        answer = interrupt({"kind": "plan", "plan": state["plan"], "table": state.get("plan_table", []),
                            "error": state.get("plan_error")})
        action = (answer or {}).get("action") if isinstance(answer, dict) else answer
        if action == "cancel":
            return {"plan_status": "cancelled", "plan_error": None}
        plan = Plan.model_validate(state["plan"])
        if isinstance(answer, dict) and answer.get("counts"):
            try:
                counts = validate_counts(pack, answer["counts"])
            except PlanError as exc:
                return {"plan_error": str(exc)}          # 이유와 함께 다시 묻는다
            if counts != plan.counts:
                plan = Plan(total=sum(counts.values()), counts=counts, source="learner",
                            rationale=plan.rationale + " (학습자가 문제 수를 고쳤습니다)")
        return {"plan": plan.model_dump(), "plan_status": "approved", "plan_error": None}

    g = StateGraph(PlannerState)
    g.add_node("draft", draft)
    g.add_node("approve", approve)
    g.add_edge(START, "draft")
    g.add_edge("draft", "approve")
    g.add_conditional_edges("approve", lambda s: END if s.get("plan_status") else "approve", ["approve", END])
    return g.compile(checkpointer=checkpointer)


__all__ = ["Plan", "PlanDraft", "PlanError", "PlannerState", "build_planner_graph", "draft_plan", "rule_plan",
           "stats_table", "summaries", "validate_counts"]
