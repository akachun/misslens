"""D50: retrieval scores, generator-context quality, index health and baseline comparison (fake embedder)."""

from __future__ import annotations

import json

import pytest

from misslens.agents.collector import load_sources
from misslens.exam_pack import load_exam_pack
from misslens.retrieval import ChunkIndex, IndexedChunk
from misslens.retrieval.evaluate import (
    Golden, compare, evaluate, index_health, load_golden, relevant, save_baseline, score_golden,
)
from test_retrieval import FakeEmbedder

PACK = load_exam_pack("ccar-f")


def golden(*items):
    return Golden.model_validate({"exam_id": "ccar-f", "items": list(items)})


@pytest.fixture
def index(tmp_path):
    idx = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path)
    idx.add([IndexedChunk("Set tool_choice to force one specific tool call.", "guide.pdf", "a", ["2.3"]),
             IndexedChunk("Prompt caching uses cache_control breakpoints to cut cost.", "https://x.skilljar.com/api/1", "b", ["4.5"]),
             IndexedChunk("Hooks: PreToolUse runs before a tool call and can block it.", "https://x.skilljar.com/cc/2", "c", ["1.5"])]
            + [IndexedChunk(f"{d.name} material for every task.", "notes.md", f"n{d.id}", [t.id for t in d.tasks][:3])
               for d in PACK.domains]
            + [IndexedChunk(f"Task statement {t} study notes, long enough to be a real chunk.", "notes.md", f"t{t}", [t])
               for t in PACK.task_ids])
    return idx


def test_relevance_is_by_content_and_source():
    g = golden({"id": "a", "query": "q", "domain": "d2", "expect": [{"text": ["tool_choice"], "source": "guide"}]})
    assert relevant("use TOOL_CHOICE here", "guide.pdf", g.items[0])
    assert not relevant("use tool_choice", "notes.md", g.items[0])


def test_scores_ranks_and_unanswerable(index):
    g = golden({"id": "hit", "query": "force one specific tool tool_choice", "domain": "d2", "task_id": "2.3",
                "expect": [{"text": ["tool_choice"]}]},
               {"id": "cache", "query": "cache_control prompt caching", "domain": "d4",
                "expect": [{"text": ["cache_control"]}]},
               {"id": "none", "query": "nothing", "domain": "d5", "expect": [{"text": ["no such phrase"]}]})
    s = score_golden(index, g)
    assert s["answerable"] == 2 and s["unanswerable"] == ["none"]
    for mode in ("plain", "coach", "task"):
        assert s["modes"][mode]["hit@1"] == 1.0 and s["modes"][mode]["mrr@10"] == 1.0
        assert set(s["modes"][mode]["by_domain"]) == {"d2", "d4"}


def test_health_flags_junk_bad_tags_and_short_chunks(index):
    rules = list(load_sources("ccar-f").sites.values())
    assert index_health(index, PACK, rules)["ok"]
    index.add([IndexedChunk("WhiteBlackRedGreenBlueYellowMagentaCyan menu", "https://x.skilljar.com/v/1", "j", []),
               IndexedChunk("Copy notes\nreal text after a chrome line", "https://x.skilljar.com/v/2", "k", []),
               IndexedChunk("tiny", "notes.md", "s", ["9.9"])])
    h = index_health(index, PACK, rules)
    assert h["junk_chunks"] == 2 and h["invalid_tags"] == ["9.9"] and h["short_chunks"] == 1
    assert not h["ok"] and not h["checks"]["no_junk_chunks"] and not h["checks"]["no_invalid_tags"]


def test_baseline_regression_is_reported(index, tmp_path):
    g = golden({"id": "hit", "query": "tool_choice", "domain": "d2", "task_id": "2.3",
                "expect": [{"text": ["tool_choice"]}]})
    rep = evaluate(index, PACK, g)
    path = save_baseline(rep, root=tmp_path / "evals")
    base = json.loads(path.read_text(encoding="utf-8"))
    assert "regressions" not in base and compare(rep.as_dict(), base, 0.05) == []
    worse = rep.as_dict()
    worse["golden"]["modes"]["plain"]["hit@5"] = base["golden"]["modes"]["plain"]["hit@5"] - 0.2
    worse["health"]["checks"]["no_junk_chunks"] = False
    regs = compare(worse, base, 0.05)
    assert any("plain hit@5" in r for r in regs) and any("no_junk_chunks" in r for r in regs)


def test_committed_golden_set_is_well_formed():
    g = load_golden("ccar-f")
    assert len(g.items) >= 30 and len({i.id for i in g.items}) == len(g.items)
    assert {i.domain for i in g.items} >= {d.id for d in PACK.domains}
    assert all(i.task_id is None or i.task_id in PACK.task_ids for i in g.items)
    assert sum(any("가" <= ch <= "힣" for ch in i.query) for i in g.items) >= 8   # Korean questions
