"""리뷰: 재질문 결과를 저장하고 진단 화면을 만든다(ARCHITECTURE §4.2).

판정은 ledger가 한다(D28). 이 모듈은 입력을 준비하고 결과를 읽어 올 뿐이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from misslens.agents.grader.reask import ReaskItem, to_reask_answers
from misslens.exam_pack import ExamPack
from misslens.ledger import Ledger, ReaskAnswer

EXPLAIN_SYSTEM = """You are a study coach for a certification exam. The learner answered a practice question wrong.
Write a short explanation in {learner} (3-5 sentences): what the chosen option gets wrong, which phrase in the
question rules it out, and why the correct option fits. Refer to options by content, not letter.
{focus}"""

FOCUS = {
    "reading": "Diagnosis: a READING miss. The learner knew the concept but missed a condition in the English text. "
               "Quote that English phrase and say how to spot it next time.",
    "uncertain": "Diagnosis: UNCERTAIN. The learner got it right only by guessing in their own language. "
                 "Reinforce the concept and the deciding phrase.",
    "concept": "Diagnosis: a CONCEPT miss. The learner did not know the concept. Teach the concept briefly.",
}

LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese"}


def submit_review(ledger: Ledger, pack: ExamPack, set_id: int, items: list[ReaskItem],
                  responses: list[tuple[int | None, bool | None]]) -> None:
    """리뷰를 저장한다. 시험 언어와 학습자 언어가 같으면 items=[]와 responses=[]를 넘긴다(재질문 생략)."""
    if pack.needs_reask:
        ledger.record_review(set_id, to_reask_answers(items, responses))
        return
    misses = [a for a in ledger.set_attempts(set_id) if not a["is_correct"] and not a["invalid"]]
    ledger.record_review(set_id, [ReaskAnswer(a["id"], False, None, None) for a in misses], reask_skipped=True)


@dataclass(frozen=True)
class DiagnosisRow:
    seq: int
    attempt_id: int
    question: dict[str, Any]
    chosen_index: int | None
    is_correct: bool
    is_decoy: bool
    miss_type: str | None
    missed_phrase: str | None
    elapsed_ms: int | None
    reask_choice: int | None = None       # 원래 보기 순서 기준. None = 재질문하지 않음
    reask_confident: bool | None = None

    @property
    def reask_text(self) -> str | None:
        """'한국어 재질문: B 선택(정답) · 확신' — 판정을 가른 근거(§4.2). 재질문하지 않았으면 None."""
        if self.reask_choice is None:
            return None
        right = self.reask_choice == self.question["answer_index"]
        conf = "" if self.reask_confident is None else (" · 확신" if self.reask_confident else " · 추측")
        return f"한국어 재질문: {'ABCD'[self.reask_choice]} 선택({'정답' if right else '오답'}){conf}"


def diagnosis_rows(ledger: Ledger, set_id: int) -> list[DiagnosisRow]:
    questions = {q["id"]: q for q in ledger.set_questions(set_id)}
    rows = []
    for a in ledger.set_attempts(set_id):
        q = questions[a["question_id"]]
        rows.append(DiagnosisRow(q["seq"], a["id"], q["payload"], a["chosen_index"], bool(a["is_correct"]),
                                 bool(a["is_decoy"]), a["miss_type"], a["missed_phrase"], a["elapsed_ms"],
                                 a["reask_choice"],
                                 None if a["reask_confident"] is None else bool(a["reask_confident"])))
    return sorted(rows, key=lambda r: r.seq)


def highlight(stem: str, phrase: str | None, start: str = "[bold yellow]", end: str = "[/]") -> str:
    """지문 안에서 놓친 문구를 표시한다(대소문자 무시, 공백 차이 허용)."""
    if not phrase:
        return stem
    # 단어를 감싼 백틱은 지문과 저장된 문구 사이에서 다를 수 있다(generator schema의 _norm 참고).
    pattern = r"\s+".join(r"`?" + re.escape(w.strip("`")) + r"`?" for w in phrase.split())
    return re.sub(pattern, lambda m: f"{start}{m.group(0)}{end}", stem, count=1, flags=re.IGNORECASE)


def explain_miss(llm: Any, pack: ExamPack, row: DiagnosisRow) -> str:
    """오답 하나에 대한 코치식 설명(explainer 역할). 실패하면 저장된 해설을 쓴다."""
    from langchain_core.messages import HumanMessage, SystemMessage

    q = row.question
    learner = LANGUAGE_NAMES.get(pack.learner_language, pack.learner_language)
    chosen = "(no answer)" if row.chosen_index is None else q["options"][row.chosen_index]["text_en"]
    msg = (f"Question:\n{q['stem_en']}\n\nOptions:\n"
           + "\n".join(f"- {o['text_en']}" for o in q["options"])
           + f"\n\nCorrect option: {q['options'][q['answer_index']]['text_en']}\nLearner chose: {chosen}\n"
           + f"Key constraints: {', '.join(q['key_constraints'])}")
    if row.missed_phrase:
        msg += f"\nPhrase the learner missed: {row.missed_phrase}"
    try:
        out = llm.invoke([SystemMessage(EXPLAIN_SYSTEM.format(learner=learner, focus=FOCUS.get(row.miss_type or "", ""))),
                          HumanMessage(msg)])
        text = getattr(out, "content", out)
        if isinstance(text, list):  # content block 목록
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        return str(text).strip() or q["explanation_native"]
    except Exception:  # noqa: BLE001 - 저장된 해설은 항상 있다
        return q["explanation_native"]
