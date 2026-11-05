"""리뷰용 재질문 양식(ARCHITECTURE §4.2, D14).

양식 = 모든 오답 + decoy(정답의 약 30%, 최소 2개, 정답이 모자라면 그보다 적게)를 무작위 순서로 두고,
학습자의 언어로 보기를 섞어 보여 준다. 어느 문항이 오답이었는지는 학습자에게 알려 주지 않는다.
답은 ledger가 판정하기 전에 원래 보기 순서로 되돌린다(D28).
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
    options: list[str]   # 화면에 보이는 순서
    order: list[int]     # order[display_index] = 원래 보기 index


def decoy_count(n_correct: int, ratio: float = DECOY_RATIO, minimum: int = MIN_DECOYS) -> int:
    return min(n_correct, max(minimum, round(ratio * n_correct)))


def build_reask_form(questions: list[dict], attempts: list[dict], *, rng: random.Random | None = None,
                     ratio: float = DECOY_RATIO, minimum: int = MIN_DECOYS) -> list[ReaskItem]:
    """questions / attempts는 Ledger.set_questions / Ledger.set_attempts가 돌려준 그대로다."""
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
    """responses[i] = items[i]에 대한 (화면상 선택 index 또는 None, confident)."""
    if len(items) != len(responses):
        raise ValueError("one response per re-ask item is required")
    out = []
    for item, (choice, confident) in zip(items, responses):
        original = None if choice is None else item.order[choice]
        out.append(ReaskAnswer(item.attempt_id, item.is_decoy, original, confident))
    return out
