"""Re-ask form for review (ARCHITECTURE §4.2, D14).

Form = every miss + decoys (about 30% of correct answers, at least 2, fewer if not enough), in random
order, shown in the learner's language with options shuffled. Nothing tells the learner which items
were misses. Answers are mapped back to the original option order before the ledger judges them (D28).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from misslens.ledger import ReaskAnswer

DECOY_RATIO = 0.3
MIN_DECOYS = 2


@dataclass(frozen=True)
class ReaskItem:
    attempt_id: int
    is_decoy: bool
    stem: str
    options: list[str]   # displayed order
    order: list[int]     # order[display_index] = original option index


def decoy_count(n_correct: int, ratio: float = DECOY_RATIO, minimum: int = MIN_DECOYS) -> int:
    return min(n_correct, max(minimum, round(ratio * n_correct)))


def build_reask_form(questions: list[dict], attempts: list[dict], *, rng: random.Random | None = None,
                     ratio: float = DECOY_RATIO, minimum: int = MIN_DECOYS) -> list[ReaskItem]:
    """questions / attempts as returned by Ledger.set_questions / Ledger.set_attempts."""
    rng = rng or random.Random()
    payload = {q["id"]: q["payload"] for q in questions if q["payload"] is not None}
    valid = [a for a in attempts if not a["invalid"]]
    misses = [a for a in valid if not a["is_correct"]]
    correct = [a for a in valid if a["is_correct"]]
    decoys = rng.sample(correct, decoy_count(len(correct), ratio, minimum))
    picked = [(a, False) for a in misses] + [(a, True) for a in decoys]
    rng.shuffle(picked)
    items = []
    for a, is_decoy in picked:
        p = payload[a["question_id"]]
        order = list(range(len(p["options"])))
        rng.shuffle(order)
        items.append(ReaskItem(a["id"], is_decoy, p["stem_native"],
                               [p["options"][i]["text_native"] for i in order], order))
    return items


def to_reask_answers(items: list[ReaskItem], responses: list[tuple[int | None, bool | None]]) -> list[ReaskAnswer]:
    """responses[i] = (displayed choice index or None, confident) for items[i]."""
    if len(items) != len(responses):
        raise ValueError("one response per re-ask item is required")
    out = []
    for item, (choice, confident) in zip(items, responses):
        original = None if choice is None else item.order[choice]
        out.append(ReaskAnswer(item.attempt_id, item.is_decoy, original, confident))
    return out
