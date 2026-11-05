"""컴파일된 LangGraph 그래프 자체에서 Mermaid 그림을 그린다.

    python scripts/draw_graph.py            # docs/graphs/{supervisor,planner,collector,coach}.mmd를 쓴다
    python scripts/draw_graph.py --print    # supervisor 그래프를 출력한다(이전 동작)

메모리 ledger, 더미 fetcher, 더미 모델을 쓴다: 모델 호출도 없고 data/ 파일도 건드리지 않는다.
커밋된 그림이 코드와 더 이상 맞지 않으면 tests/test_supervisor.py가 실패한다.
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
    def fetch(self, url):  # pragma: no cover - 그림을 그릴 때는 호출되지 않는다
        raise RuntimeError("drawing only")

    def close(self):
        pass


class _NoModel:
    def bind_tools(self, tools, **kw):
        return RunnableLambda(lambda x: x)


def _clean(text: str) -> str:
    # 서브그래프 노드 id는 "planner\3adraft"(':'를 CSS 이스케이프한 형태)로 나오는데, GitHub 렌더러가 이를 처리하지 못한다.
    text = text.replace("\\3a", "__")
    # 두 interrupt 노드(CLAUDE.md 규칙 4)를 눈에 띄게 표시한다.
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
    """supervisor 그래프(예전 호출 코드를 위해 남겨 둔다)."""
    return diagrams(exam_id)["supervisor"]


if __name__ == "__main__":
    if "--print" in sys.argv:
        print(mermaid())
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in diagrams().items():
            (OUT_DIR / f"{name}.mmd").write_text(text + "\n", encoding="utf-8")
            print(f"wrote {OUT_DIR / (name + '.mmd')}")
