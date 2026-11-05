"""Hybrid retrieval: tokenizer, RRF, index search/filter/persistence, RetrievalContext (ARCHITECTURE §8)."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from misslens.agents.generator.context import RetrievalContext, TaskStatementContext, reference_block, Chunk
from misslens.exam_pack import load_exam_pack
from misslens.retrieval import ChunkIndex, IndexedChunk, rrf, tokenize

PACK = load_exam_pack("ccar-f")


class FakeEmbedder:
    """Hashed bag of words: texts that share tokens are close. Deterministic, no model download."""

    name = "fake-hash"
    dim = 128

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in tokenize(text):
                out[row, int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
            n = np.linalg.norm(out[row])
            if n:
                out[row] /= n
        return out


def chunk(text, tags=(), h="h0", source="doc.md"):
    return IndexedChunk(text=text, source=source, content_hash=h, task_ids=list(tags))


FILLER = [
    "Agents call tools in a loop until the task is complete and the stop reason says end_turn.",
    "Prompt caching reduces cost when the same long system prompt is reused across requests.",
    "Subagents receive only the context passed to them by the coordinator agent.",
    "Structured output with a JSON schema makes downstream parsing reliable.",
    "Claude Code reads CLAUDE.md files for project instructions at session start.",
    "에이전트는 작업이 끝날 때까지 도구를 반복해서 호출한다.",
]


# --- tokenizer --------------------------------------------------------------------------------


def test_tokenize_keeps_exam_terms_whole_and_adds_parts():
    toks = tokenize("Set tool_choice and --allowedTools in .claude/settings.json")
    assert "tool_choice" in toks and "tool" in toks and "choice" in toks
    assert "allowedtools" in toks
    assert "claude/settings.json" in toks and "settings" in toks


def test_tokenize_korean_bigrams_match_across_particles():
    a, b = set(tokenize("도구를 설계한다")), set(tokenize("도구가 중요하다"))
    assert "도구" in a and "도구" in b


# --- RRF --------------------------------------------------------------------------------------


def test_rrf_rewards_documents_ranked_by_both():
    fused = rrf([[1, 2, 3], [3, 1, 4]], [0.5, 0.5])
    order = [d for d, _ in fused]
    assert order[0] == 1          # rank 1 and 2
    assert order[1] == 3          # rank 3 and 1
    assert set(order) == {1, 2, 3, 4}


# --- index ------------------------------------------------------------------------------------


@pytest.fixture
def index(tmp_path):
    idx = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    idx.add([chunk(t, h=f"f{i}") for i, t in enumerate(FILLER)])
    return idx


def test_exact_exam_term_document_is_in_top_k(index):
    """TASK T6-a: a document with the exact exam term must come back in the top k."""
    index.add([chunk("Set tool_choice to {\"type\": \"tool\", \"name\": \"extract\"} to force one tool call.",
                     h="target")])
    hits = index.search("How do I force a specific tool with tool_choice?", k=3)
    assert any("tool_choice" in h.text for h in hits)


def test_korean_query_finds_korean_chunk(index):
    hits = index.search("도구를 반복 호출", k=2)
    assert any("도구" in h.text for h in hits)


def test_task_filter_ranks_tagged_first_then_backfills(index):
    index.add([chunk("Write tool descriptions that state inputs, outputs and boundaries.", tags=["2.1"], h="t")])
    hits = index.search("prompt caching cost", k=3, task_id="2.1")
    assert hits[0].tagged and "2.1" in hits[0].task_ids   # tagged chunk first even if the query matches less
    assert len(hits) == 3 and not any(h.tagged for h in hits[1:])  # rest backfilled from the full ranking
    assert "caching" in hits[1].text


def test_search_on_empty_index_returns_nothing(tmp_path):
    assert ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path).search("anything") == []


def test_save_and_reload_round_trip(index, tmp_path):
    index.add([chunk("MCP servers are configured in .mcp.json at project scope.", tags=["2.4"], h="mcp")])
    index.save()
    again = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    assert len(again) == len(index)
    assert again.has_content("mcp") and not again.has_content("nope")
    assert again.search(".mcp.json project scope", k=1)[0].task_ids == ("2.4",)
    assert again.tag_counts()["2.4"] == 1


def test_index_built_with_other_embedder_is_refused(index, tmp_path):
    index.save()

    class Other(FakeEmbedder):
        name = "other"

    with pytest.raises(ValueError, match="re-ingest"):
        ChunkIndex("ccar-f", Other(), root=tmp_path)


# --- context provider for the generator -------------------------------------------------------


def test_retrieval_context_returns_statement_plus_chunks(index):
    index.add([chunk("Tool descriptions are the primary mechanism the model uses to select tools.",
                     tags=["2.1"], h="d", source="https://example.com/d2")])
    ctx = RetrievalContext(index, k=2)
    chunks = ctx(PACK, "2.1")
    assert ctx.grounding == "retrieved"
    assert "2.1" in chunks[0].text                      # the task statement comes first
    assert chunks[1].source == "https://example.com/d2"  # then the tagged chunk
    assert len(chunks) == 3


def test_retrieval_context_falls_back_without_material(tmp_path):
    ctx = RetrievalContext(ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path))
    chunks = ctx(PACK, "1.1")
    assert ctx.grounding == TaskStatementContext.grounding
    assert len(chunks) == 1


def test_reference_block_escapes_source_attribute():
    block = reference_block([Chunk("body", 'a"b<c')])
    assert 'source="a&quot;b&lt;c"' in block
