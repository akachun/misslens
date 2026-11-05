"""Draw the LangGraph graphs as Mermaid, from the compiled graphs themselves.

    python scripts/draw_graph.py            # writes docs/graphs/{supervisor,planner,collector,coach}.mmd
    python scripts/draw_graph.py --print    # prints the supervisor graph (old behaviour)

Uses an in-memory ledger, a dummy fetcher and a dummy model: no model calls, no data/ files touched.
tests/test_supervisor.py fails when a committed diagram no longer matches the code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver

from misslens.agents.collector import build_collector_graph, load_sources
from misslens.agents.grader.coach import build_coach_graph, make_tools
from misslens.agents.planner import build_planner_graph
from misslens.exam_pack import load_exam_pack
from misslens.graph import Services, build_supervisor_graph
from misslens.ledger import Ledger

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "graphs"
GRAPHS = ("supervisor", "planner", "collector", "coach")


class _NoFetch:
    def fetch(self, url):  # pragma: no cover - never called while drawing
        raise RuntimeError("drawing only")

    def close(self):
        pass


class _NoModel:
    def bind_tools(self, tools, **kw):
        return RunnableLambda(lambda x: x)


def _clean(text: str) -> str:
    # Subgraph node ids come out as "planner\3adraft" (CSS-escaped ':'); GitHub's renderer chokes on them.
    text = text.replace("\\3a", "__")
    # Mark the two interrupt nodes (CLAUDE.md rule 4) so they stand out.
    text = re.sub(r"(\b(?:\w+__)?approve)\(approve\)", r"\1(approve ⏸ interrupt①)", text)
    return re.sub(r"(\b(?:\w+__)?wait_login)\(wait_login\)", r"\1(wait_login ⏸ interrupt②)", text)


def diagrams(exam_id: str = "ccar-f") -> dict[str, str]:
    pack = load_exam_pack(exam_id)
    sources = load_sources(exam_id)
    ledger = Ledger(":memory:")
    site = next(iter(sources.sites))
    svc = Services(pack=pack, ledger=ledger, make_generator=lambda: None, sources=sources,
                   fetchers={name: _NoFetch() for name in sources.sites})
    graphs = {
        "supervisor": build_supervisor_graph(svc, checkpointer=InMemorySaver()).get_graph(xray=1),
        "planner": build_planner_graph(pack, ledger, None).get_graph(),
        "collector": build_collector_graph(sources.sites[site], _NoFetch()).get_graph(),
        "coach": build_coach_graph(_NoModel(), make_tools(pack, ledger, None)).get_graph(),
    }
    return {name: _clean(g.draw_mermaid()).strip() for name, g in graphs.items()}


def mermaid(exam_id: str = "ccar-f") -> str:
    """The supervisor graph (kept for older callers)."""
    return diagrams(exam_id)["supervisor"]


if __name__ == "__main__":
    if "--print" in sys.argv:
        print(mermaid())
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in diagrams().items():
            (OUT_DIR / f"{name}.mmd").write_text(text + "\n", encoding="utf-8")
            print(f"wrote {OUT_DIR / (name + '.mmd')}")
