"""Reporter 에이전트: SERVICE F9의 현황 숫자를 ledger 통계에서만 읽어 온다(CLAUDE.md 규칙 1).

모델은 쓰지 않는다. CLI `status`, Supervisor의 status 경로, Streamlit 현황 탭이 모두 이 보고서 하나를 그리므로
모든 화면에 같은 숫자가 나온다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from misslens.exam_pack import ExamPack
from misslens.ledger import (
    Ledger, avg_time, coverage, diagnosis_agreement, generation_failures, miss_breakdown, top_missed_phrases,
)
from misslens.agents.planner import stats_table, summaries


@dataclass
class Report:
    exam: str
    tasks_total: int
    asked: list[str]
    never_asked: list[str]
    coverage_ratio: float
    domains: list[dict]                    # 영역별: weight, tasks, asked, attempts, accuracy
    concept: int
    reading: int
    uncertain: int
    reading_ratio: float | None
    baseline: float | None                 # decoy 재질문 정답률, reading_ratio 옆에 보여 준다(D31)
    avg_time_ms: float | None
    top_phrases: list[tuple[str, int]] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    agreement: dict = field(default_factory=dict)   # 학습자 라벨 대 진단(labeled, compared, agree, rate)

    def as_dict(self) -> dict:
        return asdict(self)


def build_report(pack: ExamPack, ledger: Ledger) -> Report:
    ex = pack.exam_id
    cov = coverage(ledger, ex, pack.task_ids, task_id_map=pack.task_id_map)
    mb = miss_breakdown(ledger, ex)
    t = avg_time(ledger, ex)
    ag = diagnosis_agreement(ledger, ex)
    return Report(
        exam=pack.name, tasks_total=len(pack.task_ids), asked=cov.asked, never_asked=cov.never_asked,
        coverage_ratio=cov.ratio, domains=stats_table(pack, summaries(pack, ledger)),
        concept=mb.concept, reading=mb.reading, uncertain=mb.uncertain, reading_ratio=mb.reading_ratio,
        baseline=mb.baseline, avg_time_ms=t.overall_ms, top_phrases=top_missed_phrases(ledger, ex),
        failures=generation_failures(ledger, ex, limit=5),
        agreement={"labeled": ag.labeled, "compared": ag.compared, "agree": ag.agree, "rate": ag.rate,
                   "confusion": ag.confusion})


__all__ = ["Report", "build_report"]
