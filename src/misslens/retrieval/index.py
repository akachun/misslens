"""시험별 하이브리드 색인: FAISS(dense, bge-m3) + BM25(키워드)를 RRF로 합친다(ARCHITECTURE §8, D17, D36).

data/index/<exam_id>/ 아래 파일:
  chunks.jsonl  chunk당 레코드 하나: text, source, task_ids(비어 있으면 미배정), 원본 문서의 content_hash
  faiss.index   L2 정규화 벡터에 대한 내적 색인(= cosine), 행 순서는 chunks.jsonl과 같다
  meta.json     embedder 이름과 차원(다른 embedder로 만든 색인은 거부한다)
BM25는 불러올 때와 add할 때마다 메모리에서 다시 만든다.

task_id로 검색하면: 그 세부 항목 태그가 붙은 chunk를 먼저 올린다. k개보다 적으면 나머지는 필터 없는 순위에서
채운다. 그래서 태그가 틀리거나 빠져도 생성기가 자료 없이 남지 않는다.
k개를 채우는 동안 거의 같은 chunk는 버린다(D46): Bedrock과 Google Cloud 강좌가 Claude API 강좌를 거의
그대로 반복해서, 한 문단의 사본 세 개가 다른 근거를 밀어낼 수 있기 때문이다.
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
RRF_C = 60                 # LangChain EnsembleRetriever와 같은 상수
DENSE_WEIGHT = 0.5
SPARSE_WEIGHT = 0.5
CANDIDATES = 50            # 합치기 전, retriever마다의 후보 수
DEDUPE_JACCARD = 0.8       # 앞선 결과와 이만큼 가까운(토큰 Jaccard) 결과는 건너뛴다(D46)


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
    tagged: bool  # task_id로 필터한 순위에서 왔으면 True


def rrf(rankings: list[list[int]], weights: list[float], c: int = RRF_C) -> list[tuple[int, float]]:
    """가중 reciprocal rank fusion. (id, score)를 점수순으로 돌려주며, 동점이면 먼저 나온 순서를 따른다."""
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

    # --- 저장 ---------------------------------------------------------------------------------

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

    # --- 쓰기 -------------------------------------------------------------------------------

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

    def remove(self, predicate) -> int:
        """predicate(chunk)가 참인 chunk를 버린다. 나머지의 벡터는 그대로 둔다(다시 임베딩하지 않음).
        이후 save()를 호출한다. 지운 개수를 돌려준다."""
        with self._lock:
            keep = [i for i, c in enumerate(self.chunks) if not predicate(c)]
            removed = len(self.chunks) - len(keep)
            if removed:
                vecs = self._faiss.reconstruct_n(0, self._faiss.ntotal) if self._faiss.ntotal else None
                new = faiss.IndexFlatIP(self.embedder.dim)
                if keep:
                    new.add(np.ascontiguousarray(vecs[keep], dtype=np.float32))
                self._faiss = new
                self.chunks = [self.chunks[i] for i in keep]
                self._rebuild_bm25()
            return removed

    def _rebuild_bm25(self) -> None:
        self._bm25 = BM25Okapi([tokenize(c.text) or ["_"] for c in self.chunks]) if self.chunks else None

    # --- 읽기 -------------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.chunks)

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.chunks:
            for t in c.task_ids or ["unassigned"]:
                counts[t] = counts.get(t, 0) + 1
        return counts

    def query_coverage(self, query: str) -> float:
        """질의의 정보 중 BM25가 볼 수 있는 몫(D51): 코퍼스에 있는 토큰의 idf를 전체 토큰의 idf로 나눈 값이다.
        코퍼스에 없는 토큰은 가장 드문 토큰으로 친다.

        BM25는 아는 토큰만 점수를 매기므로, 그 순위도 이 비율만큼만 믿는다. 영어 코퍼스에 영어 질의를 하면
        1.0이다(0.5/0.5 융합 그대로). "Claude Code 권한 모드는 어떤 것들이 있나요"는 0.04다: BM25는 흔한 단어
        "claude code"만 보므로, 한국어를 이해한 dense 순위를 더 이상 이기지 못한다. 한국어 질문 안의 드문 영어
        용어는 어느 정도 가중치를 유지한다("extended thinking은 …" 0.18)."""
        toks = set(tokenize(query))
        if not toks or self._bm25 is None:
            return 0.0
        idf = self._bm25.idf
        rare = max(idf.values(), default=1.0)
        seen = sum(max(idf[t], 0.0) for t in toks if t in idf)
        unseen = sum(1 for t in toks if t not in idf) * rare
        return seen / (seen + unseen) if seen + unseen > 0 else 0.0

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

    def search(self, query: str, k: int = 6, task_id: str | None = None, *, max_tagged: int | None = None,
               where=None) -> list[Hit]:
        """max_tagged는 세부 항목 태그가 붙은 chunk를 앞에 몇 개까지 둘지 정한다(나머지는 전체 관련도순, D49).
        where(chunk) -> bool은 코퍼스를 제한한다(예: 한 강좌의 레슨만)."""
        with self._lock:
            if not self.chunks:
                return []
            n = min(len(self.chunks), max(CANDIDATES, k))
            # 필터할 때는 코퍼스 전체에 순위를 매겨, 조건에 맞는 chunk가 일찍 잘리지 않게 한다.
            if task_id is not None or where is not None:
                n = len(self.chunks)
            fused = rrf([self._dense(query, n), self._sparse(query, n)],
                        [DENSE_WEIGHT, SPARSE_WEIGHT * self.query_coverage(query)])
            if where is not None:
                fused = [(i, s) for i, s in fused if where(self.chunks[i])]
            tagged = [(i, s) for i, s in fused if task_id is not None and task_id in self.chunks[i].task_ids]
            if max_tagged is not None:
                tagged = tagged[:max_tagged]
            tagged_ids = {i for i, _ in tagged}
            rest = [(i, s) for i, s in fused if i not in tagged_ids]
            out: list[Hit] = []
            kept: list[set[str]] = []
            for i, s in tagged + rest:
                if len(out) >= k:
                    break
                c = self.chunks[i]
                toks = set(tokenize(c.text))
                if any(toks and len(toks & t) / len(toks | t) > DEDUPE_JACCARD for t in kept):
                    continue
                kept.append(toks)
                out.append(Hit(i, c.text, c.source, tuple(c.task_ids), s, i in tagged_ids))
            return out
