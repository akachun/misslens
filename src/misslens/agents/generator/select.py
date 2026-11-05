"""세부 항목(task statement) 선택(ARCHITECTURE §7 '세부 항목 선택', D11).

1. 한 번도 출제되지 않은 세부 항목이 먼저 오고, 영역 가중치가 높은 것부터 둔다(약점보다 범위가 먼저).
2. 나머지는 비복원 가중 무작위 순서로 둔다:
   weight = (영역 가중치 / 영역의 세부 항목 수) x (1 - 정답률), 정답률은 ledger에서(Laplace, 최근 10개).
3. 이번 세션에서 생성에 실패한 세부 항목은 뺀다.
전체 우선순위를 돌려준다. generator는 세트가 찰 때까지 이 순서를 따라간다.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from misslens.exam_pack import ExamPack
from misslens.ledger import Ledger, coverage, laplace, task_accuracy


def select_tasks(pack: ExamPack, ledger: Ledger, *, exclude: Iterable[str] = (),
                 rng: random.Random | None = None) -> list[str]:
    rng = rng or random.Random()
    excluded = set(exclude)
    cov = coverage(ledger, pack.exam_id, pack.task_ids, task_id_map=pack.task_id_map)
    acc = task_accuracy(ledger, pack.exam_id, task_id_map=pack.task_id_map)

    order = {t: i for i, t in enumerate(pack.task_ids)}
    never = sorted((t for t in cov.never_asked if t not in excluded),
                   key=lambda t: (-pack.domain_of(t).weight, order[t]))

    pool = [t for t in cov.asked if t not in excluded]
    weights = {t: pack.task_weight(t) * (1 - (acc[t].accuracy if t in acc else laplace(0, 0))) for t in pool}
    rest: list[str] = []
    while pool:
        pick = rng.choices(pool, weights=[weights[t] for t in pool], k=1)[0]
        rest.append(pick)
        pool.remove(pick)
    return never + rest
