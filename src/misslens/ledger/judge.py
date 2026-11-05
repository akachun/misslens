"""진단 판정 표(ARCHITECTURE §4.2, CLAUDE.md rule 3)를 순수 함수로 구현한다.

ledger가 리뷰를 저장할 때 이 함수를 적용하므로 저장된 miss_type / missed_phrase는 항상
표를 따른다. 채점기도 화면 표시용으로 호출할 수 있다.
"""

from __future__ import annotations

from typing import Any, Literal

MissType = Literal["concept", "reading", "uncertain"]


def judge(*, first_correct: bool, is_decoy: bool, reask_correct: bool | None,
          reask_confident: bool | None) -> MissType | None:
    """한 번의 풀이에 대한 miss_type을 돌려준다. 판정할 대상이 아니면 None이다.

    | 대상            | 재질문 결과            | miss_type  |
    | 오답            | 정답 + 확신            | reading    |
    | 오답            | 정답 + 찍음            | uncertain  |
    | 오답            | 오답                   | concept    |
    | 오답            | 재질문 없음(같은 언어) | concept    |
    | 미끼 / 정답     | 무관                   | None       |
    """
    if is_decoy or first_correct:
        return None
    if reask_correct is None:
        return "concept"
    if not reask_correct:
        return "concept"
    return "reading" if reask_confident else "uncertain"


def missed_phrase(miss_type: MissType | None, payload: dict[str, Any], chosen_index: int | None) -> str | None:
    """학습자가 처음 고른 보기의 `violates`. reading 실수일 때만 기록한다."""
    if miss_type != "reading" or chosen_index is None:
        return None
    options = payload.get("options") or []
    if not 0 <= chosen_index < len(options):
        return None
    return options[chosen_index].get("violates")
