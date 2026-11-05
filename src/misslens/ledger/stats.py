"""모든 통계(CLAUDE.md rule 1, ARCHITECTURE §5, §7, D9).

공통 규칙:
  - 어디서나(문구 순위 포함) invalid=0인 풀이만 센다.
  - 정답률은 Laplace 평활화한다: (correct + 1) / (attempts + 2), 세부 항목별 최근 유효 풀이 10개 기준.
  - `uncertain` 실수는 오답으로 센다(첫 답이 틀렸으므로).
  - 재질문 해석 실수 비율 = reading / (reading + concept). uncertain은 양쪽에서 빼고 따로 보고한다.
  - 기준선(baseline) = 미끼(처음에 맞힌 문제)에 대한 재질문 정답률.
호출하는 쪽(Generator, Planner, Reporter)은 이 함수들을 쓰고 직접 SQL로 집계하지 않는다.
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
    """세부 항목별로 최근 `window`개의 유효 풀이를 기준으로 한다. 한 번도 출제되지 않은 세부 항목은 빠진다
    (정답률은 laplace(0, 0) = 0.5). task_id_map은 예전 팩 버전의 ID를 합친다."""
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
    """유효 풀이가 하나라도 있으면 그 세부 항목은 다룬 것으로 본다. 순서는 `task_ids`를 따른다."""
    mapping = task_id_map or {}
    seen = {mapping.get(r["task_id"], r["task_id"]) for r in ledger._query(_VALID_ATTEMPTS, (exam_id,))}
    return Coverage(asked=[t for t in task_ids if t in seen], never_asked=[t for t in task_ids if t not in seen])


@dataclass(frozen=True)
class DomainSummary:
    """영역별 범위(한 번 이상 출제된 세부 항목)와, 세부 항목마다 최근 window 기준으로 낸 정답률."""
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
    """task_domain: 현재 task_id -> 영역 id(시험 팩에서 가져옴). coverage()와 task_accuracy()로 만들므로
    Planner와 Reporter가 앱의 나머지 부분과 같은 숫자를 본다."""
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
        """reading / (reading + concept). uncertain은 양쪽에서 뺀다."""
        n = self.reading + self.concept
        return self.reading / n if n else None

    @property
    def baseline(self) -> float | None:
        """미끼에 대한 재질문 정답률."""
        return self.decoys_correct / self.decoys if self.decoys else None


def miss_breakdown(ledger: Ledger, exam_id: str, *, set_id: int | None = None) -> MissBreakdown:
    """그 시험의 리뷰한 세트 전체를 집계한다. set_id를 주면 그 세트 하나만 집계한다(세트별 기준선)."""
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


@dataclass(frozen=True)
class DiagnosisAgreement:
    """같은 오답에 대해 시스템 진단(판정 표)과 학습자가 직접 붙인 라벨을 비교한다(T10 eval, D43)."""
    labeled: int
    compared: int          # 시스템도 concept/reading이고 학습자도 concept/reading인 경우
    agree: int
    confusion: dict = field(default_factory=dict)   # "system->learner" -> 개수, 라벨이 붙은 모든 오답

    @property
    def rate(self) -> float | None:
        return self.agree / self.compared if self.compared else None


def diagnosis_agreement(ledger: Ledger, exam_id: str) -> DiagnosisAgreement:
    rows = ledger._query(
        "SELECT a.miss_type, f.learner_label FROM miss_feedback f JOIN attempts a ON a.id = f.attempt_id"
        " JOIN questions q ON q.id = a.question_id WHERE q.exam_id = ? AND a.invalid = 0", (exam_id,))
    confusion: dict[str, int] = defaultdict(int)
    compared = agree = 0
    for r in rows:
        confusion[f"{r['miss_type']}->{r['learner_label']}"] += 1
        if r["miss_type"] in ("concept", "reading") and r["learner_label"] in ("concept", "reading"):
            compared += 1
            agree += int(r["miss_type"] == r["learner_label"])
    return DiagnosisAgreement(len(rows), compared, agree, dict(confusion))


def top_missed_phrases(ledger: Ledger, exam_id: str, limit: int = 10) -> list[tuple[str, int]]:
    """가장 자주 놓친 조건 문구. 무효 풀이는 join에서 뺀다."""
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
    """한 세트의 첫 답 점수(유효 풀이 기준, 제출 직후 보여 준다)."""
    rows = ledger._query("SELECT is_correct FROM attempts WHERE set_id = ? AND invalid = 0", (set_id,))
    return SetScore(correct=sum(r["is_correct"] for r in rows), total=len(rows))


def generation_failures(ledger: Ledger, exam_id: str, limit: int = 20) -> list[dict]:
    return [dict(r) for r in ledger._query(
        "SELECT task_id, tries, reason, created_at FROM generation_failures WHERE exam_id = ? "
        "ORDER BY id DESC LIMIT ?", (exam_id, limit))]
