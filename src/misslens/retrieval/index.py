"""Per-exam hybrid index: FAISS (dense, bge-m3) + BM25 (keywords), fused with RRF (ARCHITECTURE §8, D17, D36).

Files under data/index/<exam_id>/:
  chunks.jsonl  one record per chunk: text, source, task_ids (empty = unassigned), content_hash of its document
  faiss.index   inner-product index over L2-normalized vectors (= cosine), same row order as chunks.jsonl
  meta.json     embedder name and dimension (an index built with another embedder is refused)
BM25 is rebuilt in memory on load and after each add.

Search with a task_id: chunks tagged with that task are ranked first; if they are fewer than k, the rest is
filled from the unfiltered ranking, so a wrong or missing tag does not leave the generator without material.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from misslens.bedrock import PROJECT_ROOT
from misslens.retrieval.embed import Embedder
from misslens.retrieval.tokenize import tokenize

INDEX_ROOT = PROJECT_ROOT / "data" / "index"
RRF_C = 60                 # same constant as LangChain EnsembleRetriever
DENSE_WEIGHT = 0.5
SPARSE_WEIGHT = 0.5
CANDIDATES = 50            # per retriever, before fusion


@dataclass
class IndexedChunk:
    text: str
    source: str
    content_hash: str
    task_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    text: str
    source: str
    task_ids: tuple[str, ...]
    score: float
    tagged: bool  # True if it came from the task_id-filtered ranking


def rrf(rankings: list[list[int]], weights: list[float], c: int = RRF_C) -> list[tuple[int, float]]:
    """Weighted reciprocal rank fusion. Returns (id, score) sorted by score, ties by first appearance."""
    scores: dict[int, float] = {}
    for ranking, w in zip(rankings, weights):
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + w / (rank + c)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class ChunkIndex:
    def __init__(self, exam_id: str, embedder: Embedder, root: Path = INDEX_ROOT):
        self.exam_id = exam_id
        self.embedder = embedder
        self.dir = Path(root) / exam_id
        self.chunks: list[IndexedChunk] = []
        self._faiss = faiss.IndexFlatIP(embedder.dim)
        self._bm25: BM25Okapi | None = None
        self._lock = threading.Lock()
        self._load()

    # --- persistence -----------------------------------------------------------------------

    def _load(self) -> None:
        meta_path = self.dir / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("embedder") != self.embedder.name or meta.get("dim") != self.embedder.dim:
            raise ValueError(f"{self.dir}: built with {meta.get('embedder')} ({meta.get('dim')}d), "
                             f"not {self.embedder.name} ({self.embedder.dim}d). Delete the directory and re-ingest.")
        with (self.dir / "chunks.jsonl").open(encoding="utf-8") as f:
            self.chunks = [IndexedChunk(**json.loads(line)) for line in f if line.strip()]
        self._faiss = faiss.read_index(str(self.dir / "faiss.index"))
        if self._faiss.ntotal != len(self.chunks):
            raise ValueError(f"{self.dir}: faiss has {self._faiss.ntotal} rows but chunks.jsonl has {len(self.chunks)}")
        self._rebuild_bm25()

    def save(self) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "chunks.jsonl.tmp"
            with tmp.open("w", encoding="utf-8") as f:
                for c in self.chunks:
                    f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
            faiss.write_index(self._faiss, str(self.dir / "faiss.index.tmp"))
            tmp.replace(self.dir / "chunks.jsonl")
            (self.dir / "faiss.index.tmp").replace(self.dir / "faiss.index")
            (self.dir / "meta.json").write_text(json.dumps(
                {"exam_id": self.exam_id, "embedder": self.embedder.name, "dim": self.embedder.dim,
                 "chunks": len(self.chunks)}, indent=2), encoding="utf-8")

    # --- writes ----------------------------------------------------------------------------

    def has_content(self, content_hash: str) -> bool:
        return any(c.content_hash == content_hash for c in self.chunks)

    def add(self, chunks: list[IndexedChunk]) -> None:
        if not chunks:
            return
        vecs = self.embedder.encode([c.text for c in chunks])
        if vecs.shape != (len(chunks), self.embedder.dim):
            raise ValueError(f"embedder returned shape {vecs.shape}")
        with self._lock:
            self._faiss.add(np.ascontiguousarray(vecs, dtype=np.float32))
            self.chunks.extend(chunks)
            self._rebuild_bm25()

    def _rebuild_bm25(self) -> None:
        self._bm25 = BM25Okapi([tokenize(c.text) or ["_"] for c in self.chunks]) if self.chunks else None

    # --- reads -----------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.chunks)

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.chunks:
            for t in c.task_ids or ["unassigned"]:
                counts[t] = counts.get(t, 0) + 1
        return counts

    def _dense(self, query: str, n: int) -> list[int]:
        q = self.embedder.encode([query])
        _, ids = self._faiss.search(np.ascontiguousarray(q, dtype=np.float32), n)
        return [int(i) for i in ids[0] if i >= 0]

    def _sparse(self, query: str, n: int) -> list[int]:
        toks = tokenize(query)
        if not toks or self._bm25 is None:
            return []
        scores = self._bm25.get_scores(toks)
        order = np.argsort(-scores, kind="stable")[:n]
        return [int(i) for i in order if scores[i] > 0]

    def search(self, query: str, k: int = 6, task_id: str | None = None) -> list[Hit]:
        with self._lock:
            if not self.chunks:
                return []
            n = min(len(self.chunks), max(CANDIDATES, k))
            # Rank the whole corpus when filtering by task, so tagged chunks are never cut off early.
            if task_id is not None:
                n = len(self.chunks)
            fused = rrf([self._dense(query, n), self._sparse(query, n)], [DENSE_WEIGHT, SPARSE_WEIGHT])
            tagged = [(i, s) for i, s in fused if task_id is not None and task_id in self.chunks[i].task_ids]
            tagged_ids = {i for i, _ in tagged}
            rest = [(i, s) for i, s in fused if i not in tagged_ids]
            out: list[Hit] = []
            for i, s in (tagged + rest)[:k]:
                c = self.chunks[i]
                out.append(Hit(i, c.text, c.source, tuple(c.task_ids), s, i in tagged_ids))
            return out
