"""Drive the Collector graph to the end, handing interrupt② (login wait) to a callback.

The CLI passes a callback that asks the learner and opens the login window; Streamlit (T9) will show the same
payload on the 자료 tab and resume with the stored thread_id instead.
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


def sqlite_checkpointer(path: Path = CHECKPOINTS_PATH) -> SqliteSaver:
    """Checkpoints live apart from ledger.sqlite (§5, §10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def new_thread_id(site: str) -> str:
    return f"collect-{site}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def run_collect(site: Site, sources: list[Source], fetcher: Any, on_login: OnLogin, *, checkpointer: Any,
                thread_id: str, **graph_kwargs) -> CollectState:
    """Start a new run, or continue thread_id if it already has a checkpoint (e.g. after Ctrl-C at the login wait)."""
    graph = build_collector_graph(site, fetcher, checkpointer=checkpointer, **graph_kwargs)
    cfg = run_config(thread_id)
    try:
        snap = graph.get_state(cfg)
        if snap.values and snap.next:                    # paused run: resume it
            pending = [i for t in snap.tasks for i in t.interrupts]
            arg: Any = Command(resume=on_login(pending[0].value)) if pending else None
        elif snap.values:                                # finished run with this id
            return snap.values
        else:
            arg = initial_state(site, sources)
        out = graph.invoke(arg, cfg)
        while out.get("__interrupt__"):
            out = graph.invoke(Command(resume=on_login(out["__interrupt__"][0].value)), cfg)
        return out
    finally:
        fetcher.close()
