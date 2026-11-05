"""All statistics (CLAUDE.md rule 1, ARCHITECTURE §5, §7, D9).

Common rules:
  - Only attempts with invalid=0 count, everywhere (phrase ranking included).
  - Accuracy is Laplace-smoothed: (correct + 1) / (attempts + 2), over the last 10 valid attempts per task.
  - `uncertain` misses count as wrong (they are wrong first answers).
  - Re-ask reading ratio = reading / (reading + concept); uncertain is excluded from both and reported apart.
  - Baseline = re-ask accuracy on decoys (questions answered correctly the first time).
Callers (Generator, Planner, Reporter) use these functions and never aggregate SQL themselves.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from misslens.ledger.store import Ledger

ACCURACY_WINDOW = 10

_VALID_ATTEMPTS = """
    SELECT a.id, a.is_correct, a.elapsed_ms, a.is_decoy, a.reask_choice, a.miss_type, a.set_id,
           q.task_id, q.domain, q.payload_json
    FROM attempts a
    JOIN questions q ON q.id = a.question_id
    WHERE q.exam_id = ? AND a.invalid = 0
"""


def laplace(correct: int, attempts: int) -> float:
    return (correct + 1) / (attempts + 2)


@dataclass(frozen=True)
class TaskAccuracy:
    task_id: str
    correct: int
    attempts: int

    @property
    def accuracy(self) -> float:
        return laplace(self.correct, self.attempts)


def task_accuracy(ledger: Ledger, exam_id: str, *, task_id_map: dict[str, str] | None = None,
                  window: int = ACCURACY_WINDOW) -> dict[str, TaskAccuracy]:
    """Per task, over its last `window` valid attempts. Tasks never asked are absent
    (their accuracy is laplace(0, 0) = 0.5). task_id_map merges IDs from older pack versions."""
    mapping = task_id_map or {}
    recent: dict[str, list[int]] = defaultdict(list)
    for r in ledger._query(_VALID_ATTEMPTS + " ORDER BY a.id DESC", (exam_id,)):
        tid = mapping.get(r["task_id"], r["task_id"])
        if len(recent[tid]) < window:
            recent[tid].append(r["is_correct"])
    return {tid: TaskAccuracy(tid, sum(v), len(v)) for tid, v in recent.items()}


@dataclass(frozen=True)
class Coverage:
    asked: list[str]
    never_asked: list[str]

    @property
    def ratio(self) -> float:
        total = len(self.asked) + len(self.never_asked)
        return len(self.asked) / total if total else 0.0


def coverage(ledger: Ledger, exam_id: str, task_ids: list[str], *,
             task_id_map: dict[str, str] | None = None) -> Coverage:
    """A task is covered once it has at least one valid attempt. Order follows `task_ids`."""
    mapping = task_id_map or {}
    seen = {mapping.get(r["task_id"], r["task_id"]) for r in ledger._query(_VALID_ATTEMPTS, (exam_id,))}
    return Coverage(asked=[t for t in task_ids if t in seen], never_asked=[t for t in task_ids if t not in seen])


@dataclass(frozen=True)
class DomainSummary:
    """Per domain: coverage (tasks asked at least once) and accuracy over each task's recent window."""
    domain: str
    tasks: int
    asked: int
    correct: int
    attempts: int

    @property
    def accuracy(self) -> float:
        return laplace(self.correct, self.attempts)

    @property
    def unasked_ratio(self) -> float:
        return (self.tasks - self.asked) / self.tasks if self.tasks else 0.0


def domain_summary(ledger: Ledger, exam_id: str, task_domain: dict[str, str], *,
                   task_id_map: dict[str, str] | None = None) -> dict[str, DomainSummary]:
    """task_domain: current task_id -> domain id (from the exam pack). Built from coverage() and task_accuracy(),
    so the Planner and Reporter see the same numbers as the rest of the app."""
    cov = coverage(ledger, exam_id, list(task_domain), task_id_map=task_id_map)
    acc = task_accuracy(ledger, exam_id, task_id_map=task_id_map)
    asked = set(cov.asked)
    out: dict[str, DomainSummary] = {}
    for d in dict.fromkeys(task_domain.values()):
        tids = [t for t, dd in task_domain.items() if dd == d]
        out[d] = DomainSummary(d, len(tids), sum(t in asked for t in tids),
                               sum(acc[t].correct for t in tids if t in acc),
                               sum(acc[t].attempts for t in tids if t in acc))
    return out


@dataclass(frozen=True)
class MissBreakdown:
    concept: int = 0
    reading: int = 0
    uncertain: int = 0
    decoys: int = 0
    decoys_correct: int = 0

    @property
    def reading_ratio(self) -> float | None:
        """reading / (reading + concept); uncertain excluded from both."""
        n = self.reading + self.concept
        return self.reading / n if n else None

    @property
    def baseline(self) -> float | None:
        """Re-ask accuracy on decoys."""
        return self.decoys_correct / self.decoys if self.decoys else None


def miss_breakdown(ledger: Ledger, exam_id: str, *, set_id: int | None = None) -> MissBreakdown:
    """Over all reviewed sets of the exam, or one set (per-set baseline) when set_id is given."""
    sql = _VALID_ATTEMPTS + (" AND a.set_id = ?" if set_id is not None else "")
    params = (exam_id, set_id) if set_id is not None else (exam_id,)
    counts = {"concept": 0, "reading": 0, "uncertain": 0}
    decoys = decoys_correct = 0
    for r in ledger._query(sql, params):
        if r["miss_type"] in counts:
            counts[r["miss_type"]] += 1
        if r["is_decoy"]:
            decoys += 1
            answer = json.loads(r["payload_json"])["answer_index"]
            decoys_correct += int(r["reask_choice"] == answer)
    return MissBreakdown(**counts, decoys=decoys, decoys_correct=decoys_correct)


def top_missed_phrases(ledger: Ledger, exam_id: str, limit: int = 10) -> list[tuple[str, int]]:
    """Most often missed condition phrases; invalid attempts are excluded from the join."""
    rows = ledger._query(
        "SELECT m.phrase, COUNT(*) AS n FROM missed_phrases m "
        "JOIN attempts a ON a.id = m.attempt_id JOIN questions q ON q.id = a.question_id "
        "WHERE q.exam_id = ? AND a.invalid = 0 GROUP BY m.phrase ORDER BY n DESC, m.phrase LIMIT ?",
        (exam_id, limit))
    return [(r["phrase"], r["n"]) for r in rows]


@dataclass(frozen=True)
class AvgTime:
    overall_ms: float | None
    by_domain_ms: dict[str, float] = field(default_factory=dict)


def avg_time(ledger: Ledger, exam_id: str) -> AvgTime:
    total: list[int] = []
    by_domain: dict[str, list[int]] = defaultdict(list)
    for r in ledger._query(_VALID_ATTEMPTS + " AND a.elapsed_ms IS NOT NULL", (exam_id,)):
        total.append(r["elapsed_ms"])
        by_domain[r["domain"]].append(r["elapsed_ms"])
    return AvgTime(overall_ms=sum(total) / len(total) if total else None,
                   by_domain_ms={d: sum(v) / len(v) for d, v in by_domain.items()})


@dataclass(frozen=True)
class SetScore:
    correct: int
    total: int


def set_score(ledger: Ledger, set_id: int) -> SetScore:
    """First-answer score of one set over valid attempts (shown right after submission)."""
    rows = ledger._query("SELECT is_correct FROM attempts WHERE set_id = ? AND invalid = 0", (set_id,))
    return SetScore(correct=sum(r["is_correct"] for r in rows), total=len(rows))


def generation_failures(ledger: Ledger, exam_id: str, limit: int = 20) -> list[dict]:
    return [dict(r) for r in ledger._query(
        "SELECT task_id, tries, reason, created_at FROM generation_failures WHERE exam_id = ? "
        "ORDER BY id DESC LIMIT ?", (exam_id, limit))]
