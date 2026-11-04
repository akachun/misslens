"""Review: store the re-ask results and build the diagnosis view (ARCHITECTURE §4.2).

Judging is done by the ledger (D28); this module only prepares input and reads results back.
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
    """Store a review. For same-language exams pass items=[] and responses=[] (re-ask skipped)."""
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


def diagnosis_rows(ledger: Ledger, set_id: int) -> list[DiagnosisRow]:
    questions = {q["id"]: q for q in ledger.set_questions(set_id)}
    rows = []
    for a in ledger.set_attempts(set_id):
        q = questions[a["question_id"]]
        rows.append(DiagnosisRow(q["seq"], a["id"], q["payload"], a["chosen_index"], bool(a["is_correct"]),
                                 bool(a["is_decoy"]), a["miss_type"], a["missed_phrase"], a["elapsed_ms"]))
    return sorted(rows, key=lambda r: r.seq)


def highlight(stem: str, phrase: str | None, start: str = "[bold yellow]", end: str = "[/]") -> str:
    """Mark the missed phrase inside the stem (case-insensitive, whitespace-tolerant)."""
    if not phrase:
        return stem
    pattern = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.sub(pattern, lambda m: f"{start}{m.group(0)}{end}", stem, count=1, flags=re.IGNORECASE)


def explain_miss(llm: Any, pack: ExamPack, row: DiagnosisRow) -> str:
    """Coach-style explanation for one miss (explainer role). Falls back to the stored explanation."""
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
        if isinstance(text, list):  # content blocks
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        return str(text).strip() or q["explanation_native"]
    except Exception:  # noqa: BLE001 - the stored explanation is always available
        return q["explanation_native"]
