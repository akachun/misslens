"""Third-party practice questions (learner-supplied files) -> structured bank (TASK T6-b, D39).

Input: data/sources/practice_problems/CCAR-F-set<N>-questions.md + -answers.md (CertSafari, saved by the learner).
Output: data/practice/<exam_id>/bank.jsonl, one record per question.

The bank is used ONLY as style examples for the generator (few-shot, §7) and for the copy guard below.
It is NOT put into the retrieval index: its answers are the site's, not verified, and grounding checks must not
lean on them. Questions from the bank are never shown to the learner as MissLens questions (CLAUDE.md rule 2).

Quality flags per record:
  answer_confirmed  the answer option's explanation starts with "Correct" and no other option's does
  disputed          named in the index file's caveat note ("2세트 32번") -> never used as a sample
  duplicate_of      same stem (whitespace/case-insensitive) as an earlier record -> never used as a sample
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


# --- generator hooks ----------------------------------------------------------------------------

def format_sample(q: PracticeQuestion) -> str:
    opts = "\n".join(f"{c}. {o}" for c, o in zip(LETTERS, q.options))
    return f"{q.stem}\n{opts}\nAnswer: {LETTERS[q.answer_index]}"


class PracticeSamples:
    """Per-task style examples and copy guard for the generator (D39)."""

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
        """A reason if the stem reuses a bank question of the same task: the share of the bank stem's tokens found
        in the new stem is over max_overlap. Containment, not Jaccard, so padding a copied stem does not hide it."""
        toks = set(tokenize(stem))
        if not toks:
            return None
        best = max((len(toks & t) / len(t) for t in self._all_by_task.get(task_id, []) if t), default=0.0)
        if best > self.max_overlap:
            return (f"the stem is too close to an existing practice question (overlap {best:.0%}); "
                    "write a new situation with different details")
        return None
