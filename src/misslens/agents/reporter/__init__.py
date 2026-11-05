"""Reporter agent: the status numbers of SERVICE F9, read from ledger statistics only (CLAUDE.md rule 1).

No model. The CLI `status`, the Supervisor's status route and the Streamlit 현황 tab all render this one report,
so every screen shows the same numbers.
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
    domains: list[dict]                    # per domain: weight, tasks, asked, attempts, accuracy
    concept: int
    reading: int
    uncertain: int
    reading_ratio: float | None
    baseline: float | None                 # decoy re-ask accuracy, shown next to reading_ratio (D31)
    avg_time_ms: float | None
    top_phrases: list[tuple[str, int]] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    agreement: dict = field(default_factory=dict)   # learner label vs diagnosis (labeled, compared, agree, rate)

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
