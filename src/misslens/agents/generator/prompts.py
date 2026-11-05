"""Generator and verifier prompts. Exam facts come from the exam pack only (CLAUDE.md rule 6)."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from misslens.agents.generator.context import Chunk, reference_block
from misslens.exam_pack import ExamPack, Scenario

LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese"}

GENERATOR_SYSTEM = """You write practice questions for a professional certification exam.

Rules for every question:
- Scenario-based, multiple choice, exactly 4 options, exactly ONE correct option.
- stem_en: a realistic production situation (60-180 words) ending with a clear question.
  It must contain the details (constraints) that decide the answer.
- key_constraints: 2-4 short phrases (3-12 words) copied VERBATIM from YOUR stem_en (never from the reference
  material or the task statement). They are scenario conditions a careless reader would miss (a limit, a
  "must not", a scope, a timing) and that rule out the wrong options. Write stem_en so that it contains them.
- Each wrong option must be plausible and must break exactly one key constraint; set its `violates` to that
  constraint, copied exactly. The correct option has violates=null.
- Keep options similar in length and style so length does not give the answer away.
- stem_native / text_native: faithful {learner} translation. Every key constraint must keep its full meaning
  in the translation. Do not add hints. Keep product names, API names, flags and code in English.
- explanation_native: in {learner}. Explain why the answer is right and which key constraint each wrong option
  breaks. Refer to options by their content, never by letter (option order is shuffled later).
- Only test the given task statement. Never test these out-of-scope topics:
{out_of_scope}
- Use only facts consistent with the reference material and well-established, documented behavior of the
  named technologies. Do not invent features, flags or limits."""

VERIFIER_SYSTEM = """You check a practice exam question before a learner sees it.
Use the reference material and well-established, documented facts. Be strict: if in doubt, fail it.
Judge four things:
1. answer_supported: the marked correct option is actually correct and consistent with the reference material.
2. violations_real: each wrong option really breaks the constraint named in its `violates`, and that constraint
   appears in the stem.
3. single_answer: no other option could also reasonably be judged correct.
4. details_accurate: every specific CLI flag, command, API parameter, field, hook or file name in the stem and
   options really exists with that exact spelling and behavior (in the reference material or in official
   documentation you are certain about), and each option's text is internally consistent (its command and its
   description say the same thing). An invented flag or parameter fails this check even in a wrong option,
   unless the option is explicitly about it not existing.
Give a short reason that names the failing check, if any."""


def generation_messages(pack: ExamPack, task_id: str, chunks: list[Chunk], *, scenario: Scenario | None,
                        samples: list[str], feedback: str | None) -> list:
    learner = LANGUAGE_NAMES.get(pack.learner_language, pack.learner_language)
    system = GENERATOR_SYSTEM.format(learner=learner, out_of_scope=pack.prompt_out_of_scope())
    parts = [
        f"Exam: {pack.name} (questions in {LANGUAGE_NAMES.get(pack.exam_language, pack.exam_language)}).",
        "Task statement to test:\n" + pack.prompt_task_list([task_id]),
    ]
    if scenario is not None:
        parts.append(f"Frame the question inside this exam scenario:\n{scenario.name}: {scenario.description}")
    parts.append(reference_block(chunks))
    if samples:
        joined = "\n\n---\n\n".join(samples)
        parts.append("Style examples from a third-party practice set. Match their scenario style, length and option "
                     "style only. They are NOT a source of facts (their answers are unverified): take facts only from "
                     "the reference material. Do not reuse their situations, wording or options:\n"
                     f"<style_examples>\n{joined}\n</style_examples>")
    if feedback:
        parts.append("Your previous attempt was rejected for this reason. Fix it:\n" + feedback)
    return [SystemMessage(system), HumanMessage("\n\n".join(parts))]


def verification_messages(question_json: str, chunks: list[Chunk]) -> list:
    return [SystemMessage(VERIFIER_SYSTEM),
            HumanMessage(f"{reference_block(chunks)}\n\nQuestion (JSON):\n{question_json}")]
