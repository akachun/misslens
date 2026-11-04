"""Task statement selection (ARCHITECTURE §7 '세부 항목 선택', D11).

1. Tasks never asked come first, highest domain weight first (coverage before weakness).
2. Then the rest in weighted random order without replacement:
   weight = (domain weight / tasks in domain) x (1 - accuracy), accuracy from ledger (Laplace, last 10).
3. Tasks that failed generation in this session are excluded.
Returns the whole priority order; the generator walks it until the set is full.
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
