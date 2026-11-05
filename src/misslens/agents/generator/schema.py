"""Question schema and validator (ARCHITECTURE §7, CLAUDE.md rule 2).

The model fills `QuestionDraft`; code adds task_id / domain / scenario_id and runs `validate_question`.
A draft that fails validation counts as one generation failure and is never shown or stored.
"""

from __future__ import annotations

import json
import random
import re

from pydantic import BaseModel, Field, field_validator

N_OPTIONS = 4
MIN_KEY_CONSTRAINTS, MAX_KEY_CONSTRAINTS = 2, 4


class Option(BaseModel):
    text_en: str = Field(min_length=1, description="Option text in English, as on the real exam")
    text_native: str = Field(min_length=1, description="The same option in the learner's language")
    violates: str | None = Field(
        default=None,
        description="For a wrong option: the key constraint it breaks, copied exactly from key_constraints. "
                    "null for the correct option only.")


class QuestionDraft(BaseModel):
    """What the generator model returns."""

    # Field order is generation order: key_constraints right after stem_en, BEFORE the options, so the model picks
    # phrases from the stem it just wrote and then builds each wrong option around one of them (D40). With options
    # first, it invented `violates` reasons (often from the reference material) that were not in the stem.
    stem_en: str = Field(min_length=1, description="Scenario and question in English")
    key_constraints: list[str] = Field(
        description="2-4 short phrases (3-12 words) copied character for character from stem_en above. Not from "
                    "the reference material or the task statement: every phrase must already appear in stem_en")
    stem_native: str = Field(min_length=1, description="Faithful translation of stem_en in the learner's language")
    options: list[Option] = Field(description="Exactly 4 options")
    answer_index: int = Field(description="0-based index of the single correct option")

    @field_validator("key_constraints", "options", mode="before")
    @classmethod
    def _json_string_list(cls, v):
        """Tool calling sometimes sends a list field as a JSON string ('["a", "b"]'); unwrap it (seen live)."""
        if isinstance(v, str) and v.lstrip().startswith("["):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v
    explanation_native: str = Field(
        min_length=1, description="Why the answer is right and which constraint each wrong option breaks, "
                                  "in the learner's language, without option letters")


class Question(QuestionDraft):
    """Validated question stored as questions.payload_json."""

    task_id: str
    domain: str
    scenario_id: str | None = None
    grounding: str = "retrieved"  # or "task_statement_only" until Phase 3 retrieval exists


class QuestionInvalid(ValueError):
    pass


# Curly quotes become straight and code backticks disappear before comparing: the model often writes
# `stop_reason` is "tool_use" in the stem and stop_reason is "tool_use" in key_constraints (seen live, 2026-11-05).
_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "`": None})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip().lower()


def validation_errors(d: QuestionDraft) -> list[str]:
    errs: list[str] = []
    if len(d.options) != N_OPTIONS:
        errs.append(f"need exactly {N_OPTIONS} options, got {len(d.options)}")
    if not 0 <= d.answer_index < len(d.options):
        errs.append(f"answer_index {d.answer_index} out of range")
        return errs
    kcs = [k.strip() for k in d.key_constraints]
    if not MIN_KEY_CONSTRAINTS <= len(kcs) <= MAX_KEY_CONSTRAINTS:
        errs.append(f"need {MIN_KEY_CONSTRAINTS}-{MAX_KEY_CONSTRAINTS} key_constraints, got {len(kcs)}")
    if len({_norm(k) for k in kcs}) != len(kcs):
        errs.append("duplicate key_constraints")
    stem = _norm(d.stem_en)
    for k in kcs:
        if _norm(k) not in stem:
            errs.append(f"key_constraint not found verbatim in stem_en: {k!r}")
    if len({_norm(o.text_en) for o in d.options}) != len(d.options):
        errs.append("duplicate option texts")
    kc_set = {_norm(k) for k in kcs}
    for i, o in enumerate(d.options):
        if i == d.answer_index:
            if o.violates is not None:
                errs.append("the correct option must have violates=null")
        elif o.violates is None:
            errs.append(f"wrong option {i} has violates=null")
        elif _norm(o.violates) not in kc_set:
            errs.append(f"option {i} violates {o.violates!r}, which is not one of key_constraints")
    return errs


def validate_question(draft: QuestionDraft, *, task_id: str, domain: str, scenario_id: str | None,
                      grounding: str) -> Question:
    errs = validation_errors(draft)
    if errs:
        raise QuestionInvalid("; ".join(errs))
    # Store violates exactly as the matching key_constraint (case/whitespace may differ in the draft).
    by_norm = {_norm(k): k.strip() for k in draft.key_constraints}
    options = [o.model_copy(update={"violates": None if o.violates is None else by_norm[_norm(o.violates)]})
               for o in draft.options]
    return Question(**draft.model_dump(exclude={"options", "key_constraints"}),
                    options=options, key_constraints=[k.strip() for k in draft.key_constraints],
                    task_id=task_id, domain=domain, scenario_id=scenario_id, grounding=grounding)


def shuffle_options(q: Question, rng: random.Random) -> Question:
    """Models tend to put the answer first; reorder options so its position is uniform."""
    order = list(range(len(q.options)))
    rng.shuffle(order)
    return q.model_copy(update={"options": [q.options[i] for i in order],
                                "answer_index": order.index(q.answer_index)})
