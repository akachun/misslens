"""실제 색인에 대한 검색 평가: golden 질의 점수, 생성기 컨텍스트 품질, 색인 건강(D50).

청킹, 정리, 태깅, 순위를 바꾼 뒤 실행해 저장된 baseline과 비교한다:

    python -m misslens.cli retrieval-eval                 # 보고. 회귀나 건강 검사 실패가 있으면 exit 1
    python -m misslens.cli retrieval-eval --save-baseline # 현재 점수를 새 baseline으로 받아들인다
    pytest -m realdata                                    # 같은 검사를 테스트로 실행

Golden 질의(evals/<exam_id>/retrieval.yaml)는 chunk id가 아니라 내용으로 관련성을 판단하므로 다시 청킹해도
유지된다: 모든 `text` 문구가 결과에 들어 있고(대소문자 무시), `source`가 주어졌다면 결과의 출처에 그것이
포함될 때 관련 있는 결과로 본다. 색인에 관련 chunk가 없는 항목은 점수를 매기지 않고 답할 수 없음으로 보고한다.

순위 방식 세 가지는 호출하는 쪽을 그대로 따른다:
  plain  세부 항목 필터 없음(학습자의 자유 질문)
  coach  세부 항목 태그가 붙은 것을 먼저 두되 최대 2개, 나머지는 관련도순(coach search_material, D49)
  task   세부 항목 태그가 붙은 것을 먼저, 제한 없음, k=6(Generator RetrievalContext)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from misslens.bedrock import PROJECT_ROOT
from misslens.retrieval.tokenize import tokenize

EVALS_DIR = PROJECT_ROOT / "evals"
K = 10
GEN_K = 6
COACH_MAX_TAGGED = 2
SHORT_CHARS = 40
THIN_TASK_CHUNKS = 5
DUP_JACCARD = 0.8
# 색인에 절대 들어가면 안 되는 페이지 장식 문구(collector 규칙은 바뀔 수 있다. 아래는 알려진 문제 문구다).
JUNK_PATTERNS = [r"WhiteBlackRedGreenBlueYellowMagentaCyan", r"Keyboard Shortcuts(Enabled|Disabled)",
                 r"Press shift question mark", r"Copy full course notes for LLMs", r"^Open in Claude\s*$",
                 r"Uh oh! Something went wrong"]


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: list[str] = Field(default_factory=list)
    source: str | None = None


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    query: str
    domain: str
    task_id: str | None = None
    expect: list[Expect] = Field(min_length=1)    # 대안 중 하나라도 맞으면 그 chunk는 관련 있다


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_id: str
    items: list[GoldenItem]


def load_golden(exam_id: str, root: Path = EVALS_DIR) -> Golden:
    return Golden.model_validate(yaml.safe_load((Path(root) / exam_id / "retrieval.yaml").read_text(encoding="utf-8")))


def baseline_path(exam_id: str, root: Path = EVALS_DIR) -> Path:
    return Path(root) / exam_id / "retrieval_baseline.json"


def relevant(text: str, source: str, item: GoldenItem) -> bool:
    low = text.lower()
    return any(all(t.lower() in low for t in e.text) and (e.source is None or e.source in source)
               for e in item.expect)


def source_group(source: str) -> str:
    if "skilljar.com/" in source:
        return source.split("skilljar.com/", 1)[1].split("/", 1)[0]
    if source.startswith("http"):
        return source.split("/")[2]
    return source.split(" p.")[0]


# --- golden 질의 ---------------------------------------------------------------------------------

def _search(index: Any, item: GoldenItem, mode: str, k: int):
    if mode == "plain":
        return index.search(item.query, k=k)
    if mode == "coach":
        return index.search(item.query, k=k, task_id=item.task_id, max_tagged=COACH_MAX_TAGGED)
    return index.search(item.query, k=k, task_id=item.task_id)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def score_golden(index: Any, golden: Golden, modes: tuple[str, ...] = ("plain", "coach", "task")) -> dict:
    answerable = [it for it in golden.items if any(relevant(c.text, c.source, it) for c in index.chunks)]
    unanswerable = [it.id for it in golden.items if it not in answerable]
    out: dict = {"items": len(golden.items), "answerable": len(answerable), "unanswerable": unanswerable, "modes": {}}
    for mode in modes:
        ranks: dict[str, int | None] = {}
        diversity, dup_pairs = [], 0
        for it in answerable:
            hits = _search(index, it, mode, K)
            rank = next((i + 1 for i, h in enumerate(hits) if relevant(h.text, h.source, it)), None)
            ranks[it.id] = rank
            top = hits[:5]
            diversity.append(len({source_group(h.source) for h in top}))
            toks = [set(tokenize(h.text)) for h in top]
            dup_pairs += sum(1 for i in range(len(toks)) for j in range(i + 1, len(toks))
                             if _jaccard(toks[i], toks[j]) > DUP_JACCARD)
        n = len(answerable) or 1

        def hit(k: int, ids=None) -> float:
            sel = [i for i in ranks if ids is None or i in ids]
            return round(sum(1 for i in sel if ranks[i] is not None and ranks[i] <= k) / (len(sel) or 1), 4)

        by_domain = {}
        for d in sorted({it.domain for it in answerable}):
            ids = {it.id for it in answerable if it.domain == d}
            by_domain[d] = {"n": len(ids), "hit@5": hit(5, ids)}
        out["modes"][mode] = {
            "hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5), "hit@10": hit(10),
            "mrr@10": round(sum(1 / r for r in ranks.values() if r) / n, 4),
            "top5_sources": round(float(np.mean(diversity)) if diversity else 0.0, 3),
            "top5_dup_pairs": dup_pairs,
            "by_domain": by_domain,
            "misses": sorted(i for i, r in ranks.items() if r is None or r > 5),
        }
    return out


# --- 생성기 컨텍스트 -----------------------------------------------------------------------------

def score_task_context(index: Any, pack: Any) -> dict:
    """세부 항목마다 Generator가 실제로 쓰는 질의로 검색해, 상위 6개 중 태그된 chunk 비율과 출처 다양성을 잰다."""
    tagged_share, groups, weak = [], [], []
    for t in pack.task_ids:
        d = pack.domain_of(t)
        hits = index.search(f"{d.name}. {pack.task(t).title}", k=GEN_K, task_id=t)
        share = sum(1 for h in hits if t in h.task_ids) / (len(hits) or 1)
        tagged_share.append(share)
        groups.append(len({source_group(h.source) for h in hits}))
        if share < 0.5:
            weak.append(t)
    return {"tasks": len(pack.task_ids), "avg_tagged_share": round(float(np.mean(tagged_share)), 4),
            "avg_source_groups": round(float(np.mean(groups)), 3), "tasks_below_half_tagged": weak}


# --- 색인 건강 -----------------------------------------------------------------------------------

def index_health(index: Any, pack: Any, site_rules: list[Any] = ()) -> dict:
    junk = [re.compile(p, re.M) for p in JUNK_PATTERNS]
    rule_lines = [re.compile(p) for s in site_rules for p in s.drop_lines]
    rule_starts = [re.compile(b.start) for s in site_rules for b in s.drop_blocks]
    valid = set(pack.task_ids)
    chunks = index.chunks
    n = len(chunks)
    short = sum(1 for c in chunks if len(c.text.strip()) < SHORT_CHARS)
    junk_hits = 0
    for c in chunks:
        lines = [line.strip() for line in c.text.splitlines()]
        if any(p.search(c.text) for p in junk) or any(p.fullmatch(line) for p in rule_lines + rule_starts
                                                        for line in lines if line):
            junk_hits += 1
    bad_tags = sorted({t for c in chunks for t in c.task_ids if t not in valid})
    per_task = {t: 0 for t in pack.task_ids}
    for c in chunks:
        for t in c.task_ids:
            if t in per_task:
                per_task[t] += 1
    digests = [hashlib.sha256(" ".join(c.text.lower().split()).encode()).hexdigest() for c in chunks]
    exact_dups = n - len(set(digests))
    vec_ok, norm_dev = True, 0.0
    if n:
        sample = np.linspace(0, n - 1, num=min(n, 50), dtype=int)
        vecs = np.stack([index._faiss.reconstruct(int(i)) for i in sample])
        norm_dev = float(np.max(np.abs(np.linalg.norm(vecs, axis=1) - 1.0)))
        vec_ok = index._faiss.ntotal == n and norm_dev < 1e-3
    groups: dict[str, int] = {}
    for c in chunks:
        g = source_group(c.source)
        groups[g] = groups.get(g, 0) + 1
    checks = {
        "vectors_match_chunks": vec_ok,
        "no_junk_chunks": junk_hits == 0,
        "no_invalid_tags": not bad_tags,
        "no_short_chunks": short == 0,
        "every_task_has_material": all(v > 0 for v in per_task.values()),
    }
    return {"chunks": n, "short_chunks": short, "junk_chunks": junk_hits, "invalid_tags": bad_tags,
            "exact_duplicate_chunks": exact_dups, "unassigned_ratio": round(sum(1 for c in chunks if not c.task_ids)
                                                                           / (n or 1), 4),
            "thin_tasks": sorted(t for t, v in per_task.items() if v < THIN_TASK_CHUNKS),
            "max_vector_norm_deviation": round(norm_dev, 6), "sources": dict(sorted(groups.items())),
            "checks": checks, "ok": all(checks.values())}


# --- 보고와 baseline -----------------------------------------------------------------------------

@dataclass
class RetrievalReport:
    exam_id: str
    golden: dict
    task_context: dict
    health: dict
    regressions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


GUARDED = [("golden", "plain", "hit@5"), ("golden", "plain", "mrr@10"), ("golden", "coach", "hit@5"),
           ("golden", "task", "hit@5"), ("task_context", None, "avg_tagged_share")]


def _get(d: dict, section: str, mode: str | None, metric: str) -> float | None:
    v = d.get(section, {})
    if mode:
        v = v.get("modes", {}).get(mode, {})
    return v.get(metric)


def compare(report: dict, baseline: dict | None, max_drop: float) -> list[str]:
    """baseline보다 max_drop 넘게 떨어진 점수와, 실패한 건강 검사."""
    out = [f"health: {k} failed" for k, ok in report["health"]["checks"].items() if not ok]
    if baseline:
        for section, mode, metric in GUARDED:
            new, old = _get(report, section, mode, metric), _get(baseline, section, mode, metric)
            if new is not None and old is not None and new < old - max_drop:
                out.append(f"{section}{'/' + mode if mode else ''} {metric}: {old:.3f} -> {new:.3f}")
        if report["golden"]["answerable"] < baseline["golden"]["answerable"]:
            out.append(f"golden answerable: {baseline['golden']['answerable']} -> {report['golden']['answerable']}")
    return out


def evaluate(index: Any, pack: Any, golden: Golden, site_rules: list[Any] = (), baseline: dict | None = None,
             max_drop: float = 0.05) -> RetrievalReport:
    rep = RetrievalReport(pack.exam_id, score_golden(index, golden), score_task_context(index, pack),
                          index_health(index, pack, site_rules))
    rep.regressions = compare(rep.as_dict(), baseline, max_drop)
    return rep


def load_baseline(exam_id: str, root: Path = EVALS_DIR) -> dict | None:
    p = baseline_path(exam_id, root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_baseline(report: RetrievalReport, root: Path = EVALS_DIR) -> Path:
    p = baseline_path(report.exam_id, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = report.as_dict()
    d.pop("regressions", None)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p
