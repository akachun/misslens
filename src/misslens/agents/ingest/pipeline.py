"""Ingest: document -> chunks -> task tags -> index + materials row (ARCHITECTURE §3, SERVICE F1).

Order matters for consistency: the index is written first, then the materials row. A duplicate is detected
from the index (content_hash), so a crash between the two steps never leaves a materials row without chunks.
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

CHUNK_SIZE = 1200   # characters; ~250-300 English tokens, several fit in one generator prompt
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
