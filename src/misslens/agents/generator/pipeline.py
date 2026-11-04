"""Question generation pipeline (ARCHITECTURE §4.1, §7; CLAUDE.md rule 2).

Per task: draft (generator model) -> validator -> grounding check (verifier model).
  - 1 initial try + 2 regenerations; the failure reason is fed back into the next try.
  - 3 failures -> generation_failures row, task excluded for this session, move to the next task.
  - 2 tasks in a row failing -> stop the set.
Only questions that pass both checks are stored (ledger.add_question) or shown.
QuestionStream runs this in one background thread (sequential calls, D13) and hands questions over
through a queue so the learner can start on the first one while the rest are generated.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from misslens.agents.generator.context import ContextProvider, TaskStatementContext
from misslens.agents.generator.prompts import generation_messages, verification_messages
from misslens.agents.generator.schema import (
    Question,
    QuestionDraft,
    QuestionInvalid,
    shuffle_options,
    validate_question,
)
from misslens.agents.generator.select import select_tasks
from misslens.exam_pack import ExamPack
from misslens.ledger import Ledger
from misslens.llm import LLMCallError, get_llm

logger = logging.getLogger(__name__)

MAX_TRIES = 3                  # initial + 2 regenerations
MAX_CONSECUTIVE_TASK_FAILURES = 2


class Verdict(BaseModel):
    answer_supported: bool = Field(description="The marked answer is correct and supported")
    violations_real: bool = Field(description="Each wrong option really breaks its named constraint")
    single_answer: bool = Field(description="No other option could also be correct")
    details_accurate: bool = Field(description="No invented flags/parameters; options internally consistent")
    reason: str = Field(description="Short reason; name the failing check if any")

    @property
    def passed(self) -> bool:
        return self.answer_supported and self.violations_real and self.single_answer and self.details_accurate


@dataclass
class TaskResult:
    task_id: str
    question: Question | None
    tries: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Event:
    kind: Literal["ready", "failed", "stopped", "done"]
    seq: int | None = None
    question_id: int | None = None
    question: Question | None = None
    message: str = ""


class Generator:
    """One instance per learning session: `failed_tasks` is the session's exclusion list (§7 step 3)."""

    def __init__(self, pack: ExamPack, ledger: Ledger, *, gen_llm: Any = None, verify_llm: Any = None,
                 context: ContextProvider | None = None, samples: list[str] | None = None,
                 rng: random.Random | None = None):
        self.pack = pack
        self.ledger = ledger
        self.gen_llm = gen_llm if gen_llm is not None else get_llm("generator")
        self.verify_llm = verify_llm if verify_llm is not None else get_llm("verifier")
        self.context = context or TaskStatementContext()
        self.samples = samples or []
        self.rng = rng or random.Random()
        self.failed_tasks: set[str] = set()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Stop after the current call (Ctrl-C in the CLI); no further ledger writes."""
        self._cancel.set()

    # --- one task -------------------------------------------------------------------------

    def generate_for_task(self, task_id: str) -> TaskResult:
        domain = self.pack.domain_of(task_id)
        scenarios = self.pack.scenarios_for_domain(domain.id)
        scenario = self.rng.choice(scenarios) if scenarios else None
        chunks = self.context(self.pack, task_id)
        result = TaskResult(task_id, None, 0)
        feedback: str | None = None
        for _ in range(MAX_TRIES):
            result.tries += 1
            try:
                draft = self.gen_llm.with_structured_output(QuestionDraft).invoke(generation_messages(
                    self.pack, task_id, chunks, scenario=scenario, samples=self.samples, feedback=feedback))
                if draft is None:  # tool calling: the model answered without calling the tool
                    raise QuestionInvalid("model returned no structured output")
                if isinstance(draft, dict):
                    draft = QuestionDraft.model_validate(draft)
                question = validate_question(draft, task_id=task_id, domain=domain.id,
                                             scenario_id=scenario.id if scenario else None,
                                             grounding=self.context.grounding)
            except QuestionInvalid as exc:
                feedback = f"validator: {exc}"
            except (ValidationError, LLMCallError) as exc:
                feedback = f"generation: {type(exc).__name__}: {str(exc)[:300]}"
            else:
                verdict = self._verify(question, chunks)
                if verdict is None:
                    feedback = "grounding check: verifier call failed"
                elif verdict.passed:
                    result.question = shuffle_options(question, self.rng)
                    return result
                else:
                    feedback = (f"grounding check failed (answer_supported={verdict.answer_supported}, "
                                f"violations_real={verdict.violations_real}, single_answer={verdict.single_answer}, "
                                f"details_accurate={verdict.details_accurate}): {verdict.reason}")
            result.reasons.append(feedback)
            logger.info("task %s try %d rejected: %s", task_id, result.tries, feedback)
        return result

    def _verify(self, question: Question, chunks) -> Verdict | None:
        try:
            out = self.verify_llm.with_structured_output(Verdict).invoke(
                verification_messages(question.model_dump_json(indent=2), chunks))
            return Verdict.model_validate(out) if isinstance(out, dict) else out
        except (ValidationError, LLMCallError) as exc:
            logger.warning("verifier failed: %s", exc)
            return None

    # --- one set --------------------------------------------------------------------------

    def run_set(self, set_id: int, n: int, emit) -> None:
        """Fill set `set_id` with up to n verified questions, calling emit(Event) as they become ready.

        `seq` numbers every slot (ready or failed) so it stays unique; only ready questions count toward n.
        """
        seq = 0
        ready = 0
        consecutive_failures = 0
        candidates = select_tasks(self.pack, self.ledger, exclude=self.failed_tasks, rng=self.rng)
        for task_id in candidates:
            if ready >= n:
                break
            if self._cancel.is_set():
                emit(Event("stopped", message="생성을 취소했습니다."))
                return
            try:
                res = self.generate_for_task(task_id)
            except Exception as exc:  # noqa: BLE001 - never leave the learner waiting forever (§4.1)
                if self._cancel.is_set():
                    emit(Event("stopped", message="생성을 취소했습니다."))
                    return
                logger.exception("generation crashed for task %s", task_id)
                self.failed_tasks.add(task_id)
                self.ledger.add_question(set_id, seq=seq, task_id=task_id, domain=self.pack.domain_of(task_id).id,
                                         blueprint_version=self.pack.version, payload=None, status="failed")
                emit(Event("failed", seq=seq, message=f"{task_id}: {type(exc).__name__}: {exc}"))
                seq += 1
                consecutive_failures += 1
            else:
                if self._cancel.is_set():
                    emit(Event("stopped", message="생성을 취소했습니다."))
                    return
                if res.question is None:
                    self.failed_tasks.add(task_id)
                    self.ledger.record_generation_failure(self.pack.exam_id, task_id, res.tries,
                                                          " | ".join(res.reasons)[:2000])
                    consecutive_failures += 1
                else:
                    qid = self.ledger.add_question(set_id, seq=seq, task_id=task_id, domain=res.question.domain,
                                                   blueprint_version=self.pack.version,
                                                   payload=res.question.model_dump())
                    emit(Event("ready", seq=seq, question_id=qid, question=res.question))
                    seq += 1
                    ready += 1
                    consecutive_failures = 0
            if consecutive_failures >= MAX_CONSECUTIVE_TASK_FAILURES:
                emit(Event("stopped", message="연속 2개 세부 항목에서 문제 생성에 실패해 세트를 중단했습니다."))
                return
        emit(Event("done", message=f"{ready}문제 준비 완료"))


class QuestionStream:
    """Background generation; the consumer pulls Events in order with next()."""

    def __init__(self, generator: Generator, set_id: int, n: int):
        self._q: queue.Queue[Event] = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(generator, set_id, n), daemon=True)

    def _run(self, generator: Generator, set_id: int, n: int) -> None:
        try:
            generator.run_set(set_id, n, self._q.put)
        except Exception as exc:  # noqa: BLE001
            logger.exception("generation thread crashed")
            self._q.put(Event("stopped", message=f"생성 중 오류로 중단했습니다: {type(exc).__name__}: {exc}"))

    def start(self) -> "QuestionStream":
        self._thread.start()
        return self

    def next(self, timeout: float | None = None) -> Event:
        return self._q.get(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)
