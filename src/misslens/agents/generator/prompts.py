"""Generator와 verifier 프롬프트. 시험 정보는 시험 팩에서만 가져온다(CLAUDE.md 규칙 6)."""

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
  material or the task statement). Each one must be a REQUIREMENT or LIMIT the solution has to satisfy, stated
  by the people who own the problem (a "must"/"must not", "only", "at most", "within", "without", a scope, a
  timing). NOT allowed as key constraints: a proposal or opinion that the question asks you to evaluate (e.g.
  what an engineer suggests), an observation, diagnosis or symptom ("descriptions are causing misrouting",
  "results are inconsistent"), or a background fact about the setup. If the stem needs such a statement, also
  state the requirement it implies and use that requirement. Write stem_en so that it contains them.
- Each wrong option must be plausible and must be wrong BECAUSE it breaks exactly one key constraint; set its
  `violates` to that constraint, copied exactly. Check: a reader who notices that requirement rejects the option
  for that reason. The correct option has violates=null.
- Exactly one option may be defensible. Do not use as a wrong option anything the reference material presents
  as a valid approach in this situation (for example a temporary or alternative strategy it recommends).
- The correct option must not simply repeat the stem's wording; it must be found by applying the requirements.
  Each wrong option must reflect a different misconception (no two wrong options with the same mistake).
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
2. violations_real: for each wrong option, (a) the phrase in `violates` appears in the stem AND is a requirement or
   limit the solution must satisfy, not a proposal being evaluated, an observation, a diagnosis, a symptom or a
   background fact; and (b) that requirement is the actual reason the option is wrong. Fail it if any wrong option
   points at such a non-requirement phrase or is wrong for a different reason.
3. single_answer: no other option could also reasonably be judged correct, including options the reference
   material presents as a valid approach in this situation.
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
