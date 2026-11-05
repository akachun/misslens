"""문제 생성 파이프라인(ARCHITECTURE §4.1, §7; CLAUDE.md 규칙 2).

세부 항목마다: 초안(generator 모델) -> validator -> 근거 검증(verifier 모델).
  - 최초 1회 + 재생성 2회. 실패 이유는 다음 시도에 피드백으로 넣는다.
  - 3회 실패 -> generation_failures 행을 남기고, 이번 세션에서 그 세부 항목을 빼고, 다음 세부 항목으로 간다.
  - 세부 항목 2개가 연속으로 실패 -> 세트를 멈춘다.
두 검사를 모두 통과한 문제만 저장(ledger.add_question)하거나 보여 준다.
QuestionStream은 이 과정을 백그라운드 스레드 하나에서 실행하고(순차 호출, D13) 문제를 queue로 넘긴다.
그래서 학습자는 나머지가 생성되는 동안 첫 문제부터 풀 수 있다.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from collections.abc import Callable
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

MAX_TRIES = 3                  # 최초 1회 + 재생성 2회
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
    """학습 세션마다 인스턴스 하나. `failed_tasks`는 그 세션의 제외 목록이다(§7 3단계)."""

    def __init__(self, pack: ExamPack, ledger: Ledger, *, gen_llm: Any = None, verify_llm: Any = None,
                 context: ContextProvider | None = None,
                 samples: list[str] | Callable[[str], list[str]] | None = None,
                 copy_guard: Callable[[str, str], str | None] | None = None,
                 rng: random.Random | None = None):
        """samples: 고정 문체 예시, 또는 task_id -> 예시(연습 문제 은행, D39).
        copy_guard: (task_id, stem_en) -> 초안이 알려진 연습 문제를 재사용하면 거절 이유."""
        self.pack = pack
        self.ledger = ledger
        self.gen_llm = gen_llm if gen_llm is not None else get_llm("generator")
        self.verify_llm = verify_llm if verify_llm is not None else get_llm("verifier")
        self.context = context or TaskStatementContext()
        self.samples = samples or []
        self.copy_guard = copy_guard
        self.rng = rng or random.Random()
        self.failed_tasks: set[str] = set()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """현재 호출이 끝나면 멈춘다(CLI의 Ctrl-C). 이후 ledger에는 더 쓰지 않는다."""
        self._cancel.set()

    # --- 세부 항목 하나 -------------------------------------------------------------------

    def generate_for_task(self, task_id: str) -> TaskResult:
        domain = self.pack.domain_of(task_id)
        scenarios = self.pack.scenarios_for_domain(domain.id)
        scenario = self.rng.choice(scenarios) if scenarios else None
        chunks = self.context(self.pack, task_id)
        samples = self.samples(task_id) if callable(self.samples) else self.samples
        result = TaskResult(task_id, None, 0)
        feedback: str | None = None
        for _ in range(MAX_TRIES):
            result.tries += 1
            try:
                draft = self.gen_llm.with_structured_output(QuestionDraft).invoke(generation_messages(
                    self.pack, task_id, chunks, scenario=scenario, samples=samples, feedback=feedback))
                if draft is None:  # tool calling: 모델이 도구를 호출하지 않고 답했다
                    raise QuestionInvalid("model returned no structured output")
                if isinstance(draft, dict):
                    draft = QuestionDraft.model_validate(draft)
                question = validate_question(draft, task_id=task_id, domain=domain.id,
                                             scenario_id=scenario.id if scenario else None,
                                             grounding=self.context.grounding)
                copied = self.copy_guard(task_id, question.stem_en) if self.copy_guard else None
                if copied:
                    raise QuestionInvalid(copied)
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

    # --- 세트 하나 -------------------------------------------------------------------------

    def run_set(self, set_id: int, n: int, emit, plan: dict[str, int] | None = None) -> None:
        """세트 `set_id`를 검증된 문제 최대 n개로 채우고, 문제가 준비될 때마다 emit(Event)를 호출한다.

        `seq`는 모든 칸(ready든 failed든)에 번호를 매겨 고유하게 유지한다. n에는 ready 문제만 센다.
        `plan`(영역 id -> 문제 수, 승인된 Planner 계획에서 옴, §4.3)은 영역별 ready 문제 수의 상한이다.
        영역 안의 세부 항목 순서는 그대로 select_tasks를 따른다(범위 먼저, 그다음 약점).
        """
        # 재개 안전(D52): 이미 문제가 있는 세트(재시작으로 생성이 멈춘 경우)는 그 뒤에서 이어 간다. seq는 이어지고,
        # ready 문제는 n과 계획 할당량에 포함되며, 그 세부 항목은 다시 출제하지 않는다.
        existing = self.ledger.set_questions(set_id)
        seq = max((q["seq"] for q in existing), default=-1) + 1
        ready = sum(1 for q in existing if q["status"] == "ready")
        consecutive_failures = 0
        per_domain: dict[str, int] = {}
        for q in existing:
            if q["status"] == "ready":
                per_domain[q["domain"]] = per_domain.get(q["domain"], 0) + 1
        used = {q["task_id"] for q in existing}
        candidates = [t for t in select_tasks(self.pack, self.ledger, exclude=self.failed_tasks, rng=self.rng)
                      if t not in used]
        for task_id in candidates:
            if ready >= n:
                break
            domain = self.pack.domain_of(task_id).id
            if plan is not None and per_domain.get(domain, 0) >= plan.get(domain, 0):
                continue
            if self._cancel.is_set():
                emit(Event("stopped", message="생성을 취소했습니다."))
                return
            try:
                res = self.generate_for_task(task_id)
            except Exception as exc:  # noqa: BLE001 - 학습자를 끝없이 기다리게 두지 않는다(§4.1)
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
                    per_domain[domain] = per_domain.get(domain, 0) + 1
                    consecutive_failures = 0
            if consecutive_failures >= MAX_CONSECUTIVE_TASK_FAILURES:
                emit(Event("stopped", message="연속 2개 세부 항목에서 문제 생성에 실패해 세트를 중단했습니다."))
                return
        emit(Event("done", message=f"{ready}문제 준비 완료"))


class QuestionStream:
    """백그라운드 생성. 소비자는 next()로 Event를 순서대로 꺼낸다."""

    def __init__(self, generator: Generator, set_id: int, n: int, plan: dict[str, int] | None = None):
        self._q: queue.Queue[Event] = queue.Queue()
        self.generator = generator
        self.set_id = set_id
        self._thread = threading.Thread(target=self._run, args=(generator, set_id, n, plan), daemon=True)

    def _run(self, generator: Generator, set_id: int, n: int, plan: dict[str, int] | None) -> None:
        try:
            generator.run_set(set_id, n, self._q.put, plan=plan)
        except Exception as exc:  # noqa: BLE001
            logger.exception("generation thread crashed")
            self._q.put(Event("stopped", message=f"생성 중 오류로 중단했습니다: {type(exc).__name__}: {exc}"))

    def start(self) -> "QuestionStream":
        self._thread.start()
        return self

    def next(self, timeout: float | None = None) -> Event:
        return self._q.get(timeout=timeout)

    def poll(self) -> list[Event]:
        """기다리지 않고 지금 받을 수 있는 Event(UI rerun이 호출한다. 페이지를 막지 않는다)."""
        out: list[Event] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)
