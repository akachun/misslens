"""하이브리드 검색: 토크나이저, RRF, 색인 검색/필터/저장, RetrievalContext(ARCHITECTURE §8)."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from misslens.agents.generator.context import RetrievalContext, TaskStatementContext, reference_block, Chunk
from misslens.exam_pack import load_exam_pack
from misslens.retrieval import ChunkIndex, IndexedChunk, rrf, tokenize

PACK = load_exam_pack("ccar-f")


class FakeEmbedder:
    """해시한 단어 주머니: 토큰을 공유하는 텍스트끼리 가깝다. 결정적이고, 모델 다운로드가 없다."""

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


# --- 토크나이저 --------------------------------------------------------------------------------


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
    assert order[0] == 1          # 1위와 2위
    assert order[1] == 3          # 3위와 1위
    assert set(order) == {1, 2, 3, 4}


# --- 색인 ------------------------------------------------------------------------------------


@pytest.fixture
def index(tmp_path):
    idx = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    idx.add([chunk(t, h=f"f{i}") for i, t in enumerate(FILLER)])
    return idx


def test_exact_exam_term_document_is_in_top_k(index):
    """TASK T6-a: 시험 용어가 그대로 들어 있는 문서는 상위 k 안에 돌아와야 한다."""
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
    assert hits[0].tagged and "2.1" in hits[0].task_ids   # 쿼리가 덜 맞아도 태깅된 chunk가 먼저
    assert len(hits) == 3 and not any(h.tagged for h in hits[1:])  # 나머지는 전체 순위에서 채운다
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


# --- 생성기용 맥락 제공자 -------------------------------------------------------


def test_retrieval_context_returns_statement_plus_chunks(index):
    index.add([chunk("Tool descriptions are the primary mechanism the model uses to select tools.",
                     tags=["2.1"], h="d", source="https://example.com/d2")])
    ctx = RetrievalContext(index, k=2)
    chunks = ctx(PACK, "2.1")
    assert ctx.grounding == "retrieved"
    assert "2.1" in chunks[0].text                      # 세부 항목 문장이 먼저 온다
    assert chunks[1].source == "https://example.com/d2"  # 그다음 태깅된 chunk
    assert len(chunks) == 3


def test_retrieval_context_falls_back_without_material(tmp_path):
    ctx = RetrievalContext(ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path))
    chunks = ctx(PACK, "1.1")
    assert ctx.grounding == TaskStatementContext.grounding
    assert len(chunks) == 1


def test_reference_block_escapes_source_attribute():
    block = reference_block([Chunk("body", 'a"b<c')])
    assert 'source="a&quot;b&lt;c"' in block


def test_near_duplicate_chunks_from_other_courses_are_skipped(index):
    """D46: 세 강좌에 복사된 같은 강의 문단은 슬롯 세 개가 아니라 하나를 채운다."""
    base = "Use tool_choice any to force the model to call one of the provided tools in this request."
    index.add([chunk(base, h="api", source="api/lesson"),
               chunk(base + " See the Bedrock console.", h="bedrock", source="bedrock/lesson"),
               chunk(base.replace("this request", "this request today"), h="vertex", source="vertex/lesson")])
    hits = index.search("tool_choice any force tool", k=3)
    sources = [h.source for h in hits]
    assert sum(s.endswith("/lesson") for s in sources) == 1
    assert len(hits) == 3                                   # 나머지 슬롯은 다른 자료로 간다


def test_remove_keeps_other_vectors(index):
    index.add([chunk("Hooks can block tool calls before they run.", h="hook", source="drop/me")])
    n = len(index)
    assert index.remove(lambda c: c.source == "drop/me") == 1
    assert len(index) == n - 1 and all(c.source != "drop/me" for c in index.chunks)
    assert "caching" in index.search("prompt caching cost", k=1)[0].text     # 남은 벡터가 여전히 맞게 정렬된다


def test_max_tagged_leaves_room_for_untagged_and_where_filters(index):
    for i in range(4):
        index.add([chunk(f"Subagent coordinator note {i} about task tools.", tags=["1.3"], h=f"g{i}", source="guide.pdf")])
    index.add([chunk("Subagent coordinator lesson with task tools and allowed tools.", h="L",
                     source="https://anthropic-partners.skilljar.com/claude-code-in-action/1")])
    hits = index.search("subagent coordinator task tools", k=4, task_id="1.3")
    assert all(h.source == "guide.pdf" for h in hits)                     # 기본: 태깅된 것이 모든 슬롯을 채운다
    hits = index.search("subagent coordinator task tools", k=4, task_id="1.3", max_tagged=2)
    assert sum(h.tagged for h in hits) == 2 and any("skilljar" in h.source for h in hits)
    hits = index.search("subagent coordinator task tools", k=4, where=lambda c: "skilljar" in c.source)
    assert [h.source for h in hits] == ["https://anthropic-partners.skilljar.com/claude-code-in-action/1"]


def test_query_coverage_weights_bm25_by_what_it_can_see(index):
    """D51: 영어 쿼리는 BM25에 온전히 보인다(융합은 그대로). 코퍼스에 없는 한국어 단어는 가중치를 낮춘다.
    흔한 단어('the')는 아무 의미가 없고, 드문 단어('caching')는 큰 의미가 있다."""
    assert index.query_coverage("prompt caching reduces cost") == 1.0
    assert index.query_coverage("권한 모드 질문") == 0.0
    assert index.query_coverage("the 권한 모드") == 0.0
    rare = index.query_coverage("caching 권한 모드")
    assert 0.0 < rare < 1.0
    assert index.query_coverage("caching cost 권한 모드") > rare
