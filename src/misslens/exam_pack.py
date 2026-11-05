"""시험 팩 로더(SERVICE F3, ARCHITECTURE §5, §7).

exams/<exam_id>/exam.yaml이 영역, 가중치, 세부 항목의 유일한 출처다(CLAUDE.md rule 6).
코드와 프롬프트는 여기서 가져오며 절대 복사하지 않는다.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from misslens.bedrock import PROJECT_ROOT

EXAMS_DIR = PROJECT_ROOT / "exams"
WEIGHT_TOLERANCE = 0.005


class Task(BaseModel):
    # extra="forbid": YAML flow mapping({id: x, title: A, B})에서 따옴표 없이 쉼표가 든 제목은 추가 키로
    # 파싱된다. 이를 조용히 무시하다 제목 4개가 첫 쉼표에서 잘렸다(exam.yaml v3에서 수정).
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    title: str


class Domain(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    name_native: str | None = None
    weight: float = Field(gt=0, le=1)
    tasks: list[Task] = Field(min_length=1)


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    description: str
    primary_domains: list[str] = Field(min_length=1)


class PassCriteria(BaseModel):
    scaled_score: int | None = None
    scale_min: int | None = None
    scale_max: int | None = None
    questions: int | None = None
    minutes: int | None = None
    note: str | None = None


class ExamPack(BaseModel):
    model_config = ConfigDict(frozen=True, ignored_types=(cached_property,))

    exam_id: str
    name: str
    version: int = Field(ge=1)
    status: Literal["draft", "final"]
    exam_language: str
    learner_language: str
    question_format: Literal["scenario_mcq_4"]
    pass_criteria: PassCriteria = PassCriteria()
    task_id_map: dict[str, str] = {}
    scenarios: list[Scenario] = []
    out_of_scope: list[str] = []
    domains: list[Domain] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "ExamPack":
        total = sum(d.weight for d in self.domains)
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise ValueError(f"domain weights must sum to 1.0, got {total:.3f}")
        domain_ids = [d.id for d in self.domains]
        if len(domain_ids) != len(set(domain_ids)):
            raise ValueError("duplicate domain id")
        task_ids = [t.id for d in self.domains for t in d.tasks]
        dupes = sorted({t for t in task_ids if task_ids.count(t) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {dupes}")
        bad_map = sorted(v for v in self.task_id_map.values() if v not in task_ids)
        if bad_map:
            raise ValueError(f"task_id_map points to unknown task ids: {bad_map}")
        scenario_ids = [s.id for s in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("duplicate scenario id")
        bad_dom = sorted({d for s in self.scenarios for d in s.primary_domains} - set(domain_ids))
        if bad_dom:
            raise ValueError(f"scenarios reference unknown domains: {bad_dom}")
        return self

    # --- 조회 ---------------------------------------------------------------------------

    @cached_property
    def _task_index(self) -> dict[str, tuple[Domain, Task]]:
        return {t.id: (d, t) for d in self.domains for t in d.tasks}

    @property
    def task_ids(self) -> list[str]:
        return list(self._task_index)

    def task(self, task_id: str) -> Task:
        return self._lookup(task_id)[1]

    def domain_of(self, task_id: str) -> Domain:
        return self._lookup(task_id)[0]

    def _lookup(self, task_id: str) -> tuple[Domain, Task]:
        try:
            return self._task_index[task_id]
        except KeyError:
            raise KeyError(f"unknown task_id {task_id!r} in exam pack {self.exam_id} v{self.version}") from None

    def task_weight(self, task_id: str) -> float:
        """세부 항목 하나가 시험에서 차지하는 몫: 영역 가중치 / 영역의 세부 항목 수(§7, D11)."""
        d = self.domain_of(task_id)
        return d.weight / len(d.tasks)

    def current_task_id(self, task_id: str) -> str:
        """예전 버전의 task_id를 현재 ID로 바꾼다(모르는 ID는 그대로 통과)."""
        return self.task_id_map.get(task_id, task_id)

    @property
    def needs_reask(self) -> bool:
        """학습자 언어가 시험 언어와 다를 때만 학습자 언어로 다시 묻는다(§4.2)."""
        return self.exam_language != self.learner_language

    def scenarios_for_domain(self, domain_id: str) -> list[Scenario]:
        """주 영역에 `domain_id`가 들어 있는 시나리오(시나리오형 문제용)."""
        return [s for s in self.scenarios if domain_id in s.primary_domains]

    # --- 프롬프트 도우미 ----------------------------------------------------------------

    def prompt_out_of_scope(self) -> str:
        """시험에 나오지 않는 주제를 생성기 프롬프트용 글머리표 목록으로 돌려준다."""
        return "\n".join(f"- {t}" for t in self.out_of_scope)

    def prompt_task_list(self, task_ids: list[str] | None = None) -> str:
        """영역과 세부 항목을 프롬프트용 텍스트로 돌려준다(프롬프트에 직접 적지 않는다)."""
        wanted = set(task_ids) if task_ids is not None else None
        lines: list[str] = []
        for d in self.domains:
            tasks = [t for t in d.tasks if wanted is None or t.id in wanted]
            if not tasks:
                continue
            lines.append(f"{d.id}. {d.name} ({d.weight:.0%})")
            lines.extend(f"  - [{t.id}] {t.title}" for t in tasks)
        return "\n".join(lines)


def load_exam_pack(exam_id: str, exams_dir: Path = EXAMS_DIR) -> ExamPack:
    path = Path(exams_dir) / exam_id / "exam.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    pack = ExamPack.model_validate(raw)
    if pack.exam_id != exam_id:
        raise ValueError(f"{path}: exam_id {pack.exam_id!r} does not match directory {exam_id!r}")
    return pack
