"""문제 스키마와 validator(ARCHITECTURE §7, CLAUDE.md 규칙 2).

모델이 `QuestionDraft`를 채우면 코드가 task_id / domain / scenario_id를 더하고 `validate_question`을 실행한다.
검증에 실패한 초안은 생성 실패 1회로 세고, 보여 주지도 저장하지도 않는다.
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

    # 필드 순서가 곧 생성 순서다. key_constraints를 stem_en 바로 뒤, options보다 앞에 두어 모델이 방금 쓴
    # 지문에서 문구를 고른 뒤 각 오답 보기를 그중 하나에 맞춰 만들게 한다(D40). options를 먼저 두었을 때는
    # 지문에 없는 `violates` 이유를 (주로 참고 자료에서) 지어냈다.
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
        """tool calling이 가끔 list 필드를 JSON 문자열('["a", "b"]')로 보낸다. 풀어서 쓴다(실호출에서 확인)."""
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
    """검증을 통과해 questions.payload_json으로 저장되는 문제."""

    task_id: str
    domain: str
    scenario_id: str | None = None
    grounding: str = "retrieved"  # Phase 3 검색이 생기기 전에는 "task_statement_only"


class QuestionInvalid(ValueError):
    pass


# 비교하기 전에 둥근 따옴표는 곧은 따옴표로 바꾸고 코드 백틱은 지운다. 모델은 지문에는 `stop_reason` is "tool_use",
# key_constraints에는 stop_reason is "tool_use"처럼 자주 쓴다(실호출에서 확인, 2026-11-05).
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
    # violates는 일치하는 key_constraint와 똑같이 저장한다(초안에서는 대소문자·공백이 다를 수 있다).
    by_norm = {_norm(k): k.strip() for k in draft.key_constraints}
    options = [o.model_copy(update={"violates": None if o.violates is None else by_norm[_norm(o.violates)]})
               for o in draft.options]
    return Question(**draft.model_dump(exclude={"options", "key_constraints"}),
                    options=options, key_constraints=[k.strip() for k in draft.key_constraints],
                    task_id=task_id, domain=domain, scenario_id=scenario_id, grounding=grounding)


def shuffle_options(q: Question, rng: random.Random) -> Question:
    """모델은 정답을 맨 앞에 두는 경향이 있다. 정답 위치가 고르게 되도록 보기 순서를 섞는다."""
    order = list(range(len(q.options)))
    rng.shuffle(order)
    return q.model_copy(update={"options": [q.options[i] for i in order],
                                "answer_index": order.index(q.answer_index)})
