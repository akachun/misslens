"""Ingest: 문서 -> chunk -> 세부 항목 태그 -> 색인 + materials 행(ARCHITECTURE §3, SERVICE F1).

일관성을 위해 순서가 중요하다. 색인을 먼저 쓰고 그다음 materials 행을 쓴다. 중복은 색인(content_hash)으로
판별하므로 두 단계 사이에 죽어도 chunk 없는 materials 행이 남지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from misslens.agents.ingest.loaders import Document
from misslens.agents.ingest.tagger import tag_chunks
from misslens.exam_pack import ExamPack
from misslens.ledger import Ledger
from misslens.retrieval.index import ChunkIndex, IndexedChunk

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1200   # 글자 수. 영어 약 250-300 토큰, generator 프롬프트 하나에 여러 개 들어간다
CHUNK_OVERLAP = 150
MIN_CHUNK_CHARS = 40


@dataclass
class IngestResult:
    source: str
    status: str                 # 'added' | 'duplicate' | 'empty'
    chunks: int = 0
    tag_counts: dict[str, int] = field(default_factory=dict)


def split_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    return [c.strip() for c in splitter.split_text(text) if len(c.strip()) >= MIN_CHUNK_CHARS]


class Ingestor:
    def __init__(self, pack: ExamPack, ledger: Ledger, index: ChunkIndex, *, tagger_llm: Any = None):
        self.pack = pack
        self.ledger = ledger
        self.index = index
        if tagger_llm is None:
            from misslens.llm import get_llm

            tagger_llm = get_llm("tagger")
        self.tagger_llm = tagger_llm

    def ingest(self, doc: Document) -> IngestResult:
        texts = split_text(doc.text)
        if not texts:
            return IngestResult(doc.source, "empty")
        h = doc.content_hash
        if self.index.has_content(h):
            return IngestResult(doc.source, "duplicate")
        tags = tag_chunks(self.pack, texts, self.tagger_llm)
        self.index.add([IndexedChunk(text=t, source=doc.source, content_hash=h, task_ids=ids)
                        for t, ids in zip(texts, tags)])
        self.index.save()
        self.ledger.add_material(self.pack.exam_id, source_type=doc.source_type, content_hash=h,
                                 source_uri=doc.source)
        counts: dict[str, int] = {}
        for ids in tags:
            for t in ids or ["unassigned"]:
                counts[t] = counts.get(t, 0) + 1
        logger.info("ingested %s: %d chunks", doc.source, len(texts))
        return IngestResult(doc.source, "added", len(texts), counts)
