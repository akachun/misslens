"""Diagnosis table (ARCHITECTURE §4.2, CLAUDE.md rule 3) as pure functions.

The ledger applies these when it stores a review, so stored miss_type / missed_phrase always
follow the table. The grader may call them too, for display.
"""

from __future__ import annotations

from typing import Any, Literal

MissType = Literal["concept", "reading", "uncertain"]


def judge(*, first_correct: bool, is_decoy: bool, reask_correct: bool | None,
          reask_confident: bool | None) -> MissType | None:
    """Return miss_type for one attempt, or None when no judgement applies.

    | target         | re-ask result        | miss_type  |
    | miss           | correct + confident  | reading    |
    | miss           | correct + guess      | uncertain  |
    | miss           | wrong                | concept    |
    | miss           | no re-ask (same lang)| concept    |
    | decoy / correct| any                  | None       |
    """
    if is_decoy or first_correct:
        return None
    if reask_correct is None:
        return "concept"
    if not reask_correct:
        return "concept"
    return "reading" if reask_confident else "uncertain"


def missed_phrase(miss_type: MissType | None, payload: dict[str, Any], chosen_index: int | None) -> str | None:
    """`violates` of the option the learner first chose; recorded for reading misses only."""
    if miss_type != "reading" or chosen_index is None:
        return None
    options = payload.get("options") or []
    if not 0 <= chosen_index < len(options):
        return None
    return options[chosen_index].get("violates")
