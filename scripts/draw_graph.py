"""Print the Supervisor graph (with Planner and Collector subgraphs) as Mermaid, from the compiled graph itself.

    python scripts/draw_graph.py > docs/graph.mmd

Uses an in-memory ledger and a dummy fetcher: no model calls, no data/ files touched.
"""

from __future__ import annotations

import re

from langgraph.checkpoint.memory import InMemorySaver

from misslens.agents.collector import load_sources
from misslens.exam_pack import load_exam_pack
from misslens.graph import Services, build_supervisor_graph
from misslens.ledger import Ledger


class _NoFetch:
    def fetch(self, url):  # pragma: no cover - never called while drawing
        raise RuntimeError("drawing only")

    def close(self):
        pass


def mermaid(exam_id: str = "ccar-f") -> str:
    pack = load_exam_pack(exam_id)
    sources = load_sources(exam_id)
    svc = Services(pack=pack, ledger=Ledger(":memory:"), make_generator=lambda: None, sources=sources,
                   fetchers={name: _NoFetch() for name in sources.sites})
    text = build_supervisor_graph(svc, checkpointer=InMemorySaver()).get_graph(xray=1).draw_mermaid()
    # Subgraph node ids come out as "planner\3adraft" (CSS-escaped ':'); GitHub's renderer chokes on them.
    text = text.replace("\\3a", "__")
    # Mark the two interrupt nodes (CLAUDE.md rule 4) so they stand out.
    text = re.sub(r"(\w+__approve)\(approve\)", r"\1(approve ⏸ interrupt①)", text)
    return re.sub(r"(\w+__wait_login)\(wait_login\)", r"\1(wait_login ⏸ interrupt②)", text)


if __name__ == "__main__":
    print(mermaid())
