"""Collector 그래프를 끝까지 실행하고, interrupt②(로그인 대기)는 콜백에 넘긴다.

CLI는 학습자에게 묻고 로그인 창을 여는 콜백을 넘긴다. Streamlit(T9)은 대신 같은 payload를 자료 탭에 보여 주고
저장해 둔 thread_id로 재개한다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from misslens.agents.collector.graph import CollectState, build_collector_graph, initial_state, run_config
from misslens.agents.collector.sources import Site, Source
from misslens.bedrock import PROJECT_ROOT

CHECKPOINTS_PATH = PROJECT_ROOT / "data" / "checkpoints.sqlite"
OnLogin = Callable[[dict], str]  # interrupt payload -> "done" | "abort"


def sqlite_checkpointer(path: Path | None = None) -> SqliteSaver:
    """체크포인트는 ledger.sqlite와 따로 둔다(§5, §10). 샌드박스 모드에서는 data/sandbox/에 둔다(D45)."""
    if path is None:
        from misslens.paths import checkpoints_path

        path = checkpoints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def new_thread_id(site: str) -> str:
    return f"collect-{site}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def run_collect(site: Site, sources: list[Source], fetcher: Any, on_login: OnLogin, *, checkpointer: Any,
                thread_id: str, **graph_kwargs) -> CollectState:
    """새 실행을 시작한다. thread_id에 이미 체크포인트가 있으면 이어서 실행한다(예: 로그인 대기 중 Ctrl-C 뒤)."""
    graph = build_collector_graph(site, fetcher, checkpointer=checkpointer, **graph_kwargs)
    cfg = run_config(thread_id)
    try:
        snap = graph.get_state(cfg)
        if snap.values and snap.next:                    # 멈춘 실행: 재개한다
            pending = [i for t in snap.tasks for i in t.interrupts]
            arg: Any = Command(resume=on_login(pending[0].value)) if pending else None
        elif snap.values:                                # 이 id로 이미 끝난 실행
            return snap.values
        else:
            arg = initial_state(site, sources)
        out = graph.invoke(arg, cfg)
        while out.get("__interrupt__"):
            out = graph.invoke(Command(resume=on_login(out["__interrupt__"][0].value)), cfg)
        return out
    finally:
        fetcher.close()
