"""외부 연습 문제(학습자가 넣은 파일) -> 구조화된 문제 은행(TASK T6-b, D39).

입력: data/sources/practice_problems/CCAR-F-set<N>-questions.md + -answers.md(CertSafari, 학습자가 저장).
출력: data/practice/<exam_id>/bank.jsonl, 문제 하나당 레코드 하나.

은행은 생성기의 문체 예시(few-shot, §7)와 아래의 베끼기 방지에만 쓴다.
검색 색인에는 넣지 않는다: 정답이 사이트가 제시한 것이라 검증되지 않았고, 근거 검증이 여기에 기대면 안 된다.
은행의 문제를 MissLens 문제로 학습자에게 보여 주는 일은 없다(CLAUDE.md rule 2).

레코드별 품질 표시:
  answer_confirmed  정답 보기의 해설이 "Correct"로 시작하고 다른 보기의 해설은 그렇지 않다
  disputed          색인 파일의 주의 메모에 이름이 있다("2세트 32번") -> 예시로 쓰지 않는다
  duplicate_of      앞선 레코드와 문제 본문이 같다(공백·대소문자 무시) -> 예시로 쓰지 않는다
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from misslens.bedrock import PROJECT_ROOT
from misslens.retrieval.tokenize import tokenize

SOURCE_DIR = PROJECT_ROOT / "data" / "sources" / "practice_problems"
BANK_ROOT = PROJECT_ROOT / "data" / "practice"
LETTERS = "ABCD"

_Q_HEAD = re.compile(r"^## (\d+)\. (.+?)\s*$", re.M)
_SUB = re.compile(r"^Subdomain (\d+\.\d+): (.+?)\s*$", re.M)
_OPT = re.compile(r"^- \*\*([A-D])\.\*\* (.+?)\s*$", re.M)
_A_HEAD = re.compile(r"^## (\d+)번 — (?:사이트 제시 )?정답 ([A-D])\s*$", re.M)
_EXP = re.compile(r"^\*\*([A-D])\.\*\* (.+?)\s*$", re.M)
_CAVEAT = re.compile(r"(\d+)세트 (\d+)번")


class PracticeFormatError(ValueError):
    pass


@dataclass
class PracticeQuestion:
    id: str                      # "s2q07"
    set_no: int
    number: int
    task_id: str
    stem: str
    options: list[str]
    answer_index: int
    explanations: list[str]
    answer_confirmed: bool
    disputed: bool = False
    duplicate_of: str | None = None
    source: str = ""

    @property
    def usable_as_sample(self) -> bool:
        return self.answer_confirmed and not self.disputed and self.duplicate_of is None


def _blocks(text: str, head: re.Pattern) -> list[tuple[re.Match, str]]:
    heads = list(head.finditer(text))
    return [(m, text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]) for i, m in enumerate(heads)]


def _stem_key(stem: str) -> str:
    return " ".join(stem.lower().split())


def parse_set(questions_md: str, answers_md: str, set_no: int, source: str = "") -> list[PracticeQuestion]:
    answers: dict[int, tuple[int, list[str]]] = {}
    for m, body in _blocks(answers_md, _A_HEAD):
        exps = dict(_EXP.findall(body))
        if sorted(exps) != list(LETTERS):
            raise PracticeFormatError(f"set {set_no} answer {m.group(1)}: explanations for {sorted(exps)}")
        answers[int(m.group(1))] = (LETTERS.index(m.group(2)), [exps[c] for c in LETTERS])
    out: list[PracticeQuestion] = []
    for m, body in _blocks(questions_md, _Q_HEAD):
        n = int(m.group(1))
        sub = _SUB.search(body)
        opts = dict(_OPT.findall(body))
        if sub is None or sorted(opts) != list(LETTERS):
            raise PracticeFormatError(f"set {set_no} question {n}: missing subdomain or options {sorted(opts)}")
        if n not in answers:
            raise PracticeFormatError(f"set {set_no} question {n}: no answer")
        ans, exps = answers[n]
        correct = [i for i, e in enumerate(exps) if e.lower().startswith("correct")]
        out.append(PracticeQuestion(
            id=f"s{set_no}q{n:02d}", set_no=set_no, number=n, task_id=sub.group(1), stem=m.group(2),
            options=[opts[c] for c in LETTERS], answer_index=ans, explanations=exps,
            answer_confirmed=correct == [ans], source=source))
    if len(out) != len(answers):
        raise PracticeFormatError(f"set {set_no}: {len(out)} questions but {len(answers)} answers")
    return out


def parse_dir(src: Path = SOURCE_DIR, valid_task_ids: set[str] | None = None) -> list[PracticeQuestion]:
    bank: list[PracticeQuestion] = []
    for qf in sorted(src.glob("*-set*-questions.md")):
        set_no = int(re.search(r"set(\d+)", qf.name).group(1))
        af = qf.with_name(qf.name.replace("-questions.md", "-answers.md"))
        src_line = re.search(r"출처: \[[^\]]*\]\(([^)]+)\)", qf.read_text(encoding="utf-8"))
        bank += parse_set(qf.read_text(encoding="utf-8"), af.read_text(encoding="utf-8"), set_no,
                          src_line.group(1) if src_line else qf.name)
    disputed = set()
    for idx in src.glob("*index*.md"):
        disputed |= {f"s{a}q{int(b):02d}" for a, b in _CAVEAT.findall(idx.read_text(encoding="utf-8"))}
    first: dict[str, str] = {}
    for q in bank:
        q.disputed = q.id in disputed
        key = _stem_key(q.stem)
        if key in first:
            q.duplicate_of = first[key]
        else:
            first[key] = q.id
        if valid_task_ids is not None and q.task_id not in valid_task_ids:
            raise PracticeFormatError(f"{q.id}: task {q.task_id} is not in the exam pack")
    return bank


def save_bank(bank: list[PracticeQuestion], exam_id: str, root: Path = BANK_ROOT) -> Path:
    path = Path(root) / exam_id / "bank.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for q in bank:
            f.write(json.dumps(asdict(q), ensure_ascii=False) + "\n")
    return path


def load_bank(exam_id: str, root: Path = BANK_ROOT) -> list[PracticeQuestion]:
    path = Path(root) / exam_id / "bank.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [PracticeQuestion(**json.loads(line)) for line in f if line.strip()]


def summarize(bank: list[PracticeQuestion]) -> dict:
    by_task: dict[str, int] = {}
    for q in bank:
        if q.usable_as_sample:
            by_task[q.task_id] = by_task.get(q.task_id, 0) + 1
    return {"total": len(bank), "duplicates": sum(q.duplicate_of is not None for q in bank),
            "unconfirmed": sum(not q.answer_confirmed for q in bank), "disputed": sum(q.disputed for q in bank),
            "usable": sum(q.usable_as_sample for q in bank), "usable_by_task": by_task,
            "answer_letters": {c: sum(q.answer_index == i for q in bank) for i, c in enumerate(LETTERS)}}


# --- 생성기 연결부 ------------------------------------------------------------------------------

def format_sample(q: PracticeQuestion) -> str:
    opts = "\n".join(f"{c}. {o}" for c, o in zip(LETTERS, q.options))
    return f"{q.stem}\n{opts}\nAnswer: {LETTERS[q.answer_index]}"


class PracticeSamples:
    """생성기용 세부 항목별 문체 예시와 베끼기 방지(D39)."""

    def __init__(self, bank: list[PracticeQuestion], *, per_task: int = 2, rng: random.Random | None = None,
                 max_overlap: float = 0.6):
        self.per_task = per_task
        self.rng = rng or random.Random()
        self.max_overlap = max_overlap
        self._usable: dict[str, list[PracticeQuestion]] = {}
        self._all_by_task: dict[str, list[set[str]]] = {}
        for q in bank:
            if q.usable_as_sample:
                self._usable.setdefault(q.task_id, []).append(q)
            self._all_by_task.setdefault(q.task_id, []).append(set(tokenize(q.stem)))

    def __call__(self, task_id: str) -> list[str]:
        pool = self._usable.get(task_id, [])
        return [format_sample(q) for q in self.rng.sample(pool, min(self.per_task, len(pool)))]

    def copy_reason(self, task_id: str, stem: str) -> str | None:
        """문제 본문이 같은 세부 항목의 은행 문제를 재사용하면 이유를 돌려준다: 은행 문제 본문의 토큰 중 새 본문에
        들어 있는 비율이 max_overlap을 넘는 경우다. Jaccard가 아니라 포함 비율이라 베낀 본문에 내용을 덧붙여도
        숨겨지지 않는다."""
        toks = set(tokenize(stem))
        if not toks:
            return None
        best = max((len(toks & t) / len(t) for t in self._all_by_task.get(task_id, []) if t), default=0.0)
        if best > self.max_overlap:
            return (f"the stem is too close to an existing practice question (overlap {best:.0%}); "
                    "write a new situation with different details")
        return None
