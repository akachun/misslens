"""로컬 트레이스: 모델 호출, LangGraph 노드, 작업 단위(span)를 JSONL로 남긴다(D60).

- 모델 호출(kind "llm"): 모든 호출이 지나는 라우터(`misslens.llm.Router.run`)가 역할, 엔드포인트, 모델, 상태,
  지연 시간, 토큰, 전환 내역을 기록한다. 그래프 노드 안에서 불렸으면 그 노드 이름과 thread_id도 붙인다.
- 그래프 노드(kind "node"): 그래프 실행 설정에 `GraphTracer` 콜백을 붙이면 노드마다 소요 시간과 오류를 남긴다.
- 작업 단위(kind "span"): `span("generate", task_id=...)`으로 감싼 구간. 그 안의 모델 호출은 같은 trace_id를 갖는다.

저장 위치는 `<state_dir>/traces/YYYYMMDD.jsonl`이다(샌드박스는 data/sandbox/traces/). data/는 git에서 빠지고
평가용 zip에도 들어가지 않는다. 프롬프트와 응답 본문은 남기지 않는다(수집한 강의 자료가 섞이므로). 메타데이터만 쓴다.
MISSLENS_TRACE=0이면 아무것도 쓰지 않는다. LangSmith는 환경 변수로 따로 켤 수 있다(README 참고, 본문이 외부로 나감).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

TRACE_ENV = "MISSLENS_TRACE"
_scope: ContextVar[dict | None] = ContextVar("misslens_trace_scope", default=None)
_write_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get(TRACE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def trace_dir() -> Path:
    from misslens.paths import state_dir   # 호출 시점에 샌드박스 여부를 따른다

    return state_dir() / "traces"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def write(event: dict, directory: Path | None = None) -> None:
    """이벤트 한 줄을 오늘 파일에 붙인다. 트레이스 실패가 학습 흐름을 막지 않게 오류는 로그만 남긴다."""
    if not enabled():
        return
    d = directory or trace_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now(), **event}, ensure_ascii=False, default=str)
        with _write_lock, open(d / f"{datetime.now():%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logger.warning("trace write failed: %s", exc)


def ambient() -> dict:
    """지금 실행 위치: span의 trace_id·이름, LangGraph 노드 안이면 노드 이름과 thread_id."""
    out: dict[str, Any] = {}
    sc = _scope.get()
    if sc:
        out.update(trace_id=sc["trace_id"], span=sc["name"])
    try:
        from langchain_core.runnables.config import var_child_runnable_config

        cfg = var_child_runnable_config.get() or {}
    except (ImportError, LookupError):
        cfg = {}
    node = (cfg.get("metadata") or {}).get("langgraph_node")
    thread = (cfg.get("configurable") or {}).get("thread_id")
    if node:
        out["node"] = node
    if thread:
        out["thread_id"] = thread
        out.setdefault("trace_id", thread)
    return out


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict]:
    """작업 단위를 감싼다. 바깥 span이나 그래프 thread가 있으면 그 trace_id를 이어받는다.
    호출한 쪽은 결과를 `sc["attrs"]`에 더할 수 있다(예: 시도 횟수)."""
    outer = ambient()
    sc = {"trace_id": outer.get("trace_id") or uuid.uuid4().hex[:12], "name": name, "attrs": {}}
    token = _scope.set(sc)
    start = time.perf_counter()
    status, error = "ok", None
    try:
        yield sc
    except BaseException as exc:
        status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        _scope.reset(token)
        write({"kind": "span", "name": name, "trace_id": sc["trace_id"], "parent": outer.get("span"),
               "node": outer.get("node"), "status": status, "ms": round((time.perf_counter() - start) * 1000),
               "error": error, **{k: v for k, v in {**attrs, **sc["attrs"]}.items() if v is not None}})


def record_llm(role: str, *, endpoint: str | None, model: str | None, status: str, ms: int, tries: int,
               usage: dict | None, attempts: list[dict], error: str | None = None) -> None:
    write({"kind": "llm", "role": role, "endpoint": endpoint, "model": model, "status": status, "ms": ms,
           "tries": tries, "failovers": sum(1 for a in attempts if not a["status"].startswith("skipped")),
           "skipped": sum(1 for a in attempts if a["status"].startswith("skipped")),
           "input_tokens": (usage or {}).get("input_tokens"), "output_tokens": (usage or {}).get("output_tokens"),
           "attempts": attempts or None, "error": error, **ambient()})


class GraphTracer(BaseCallbackHandler):
    """LangGraph 노드의 시작·끝을 기록한다. 그래프 실행 설정의 callbacks에 붙인다(`with_tracing`)."""

    def __init__(self) -> None:
        self._starts: dict[Any, tuple[float, str, str | None]] = {}

    def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: Any, parent_run_id: Any = None,
                       tags: Any = None, metadata: dict | None = None, **kwargs: Any) -> None:
        node = (metadata or {}).get("langgraph_node")
        if node and kwargs.get("name") == node:      # 노드 자체만(노드 안의 작은 runnable은 빼고)
            self._starts[run_id] = (time.perf_counter(), node, (metadata or {}).get("thread_id"))

    def _end(self, run_id: Any, status: str, error: str | None = None) -> None:
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        t0, node, thread = started
        write({"kind": "node", "node": node, "thread_id": thread, "trace_id": thread, "status": status,
               "ms": round((time.perf_counter() - t0) * 1000), "error": error})

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._end(run_id, "ok")

    def on_chain_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        # interrupt(계획 승인·로그인 대기)도 예외로 전달된다. 실패가 아니라 멈춤으로 기록한다.
        kind = "interrupt" if type(error).__name__ == "GraphInterrupt" else "error"
        self._end(run_id, kind, None if kind == "interrupt" else f"{type(error).__name__}: {error}"[:300])


def with_tracing(config: dict) -> dict:
    """그래프 실행 설정에 노드 트레이스 콜백을 붙인다(꺼져 있으면 그대로)."""
    if not enabled():
        return config
    return {**config, "callbacks": [*(config.get("callbacks") or []), GraphTracer()]}


# --- 읽기와 요약 --------------------------------------------------------------------------------

def load_events(day: str | None = None, directory: Path | None = None) -> list[dict]:
    """day: YYYYMMDD(기본 오늘), "all"이면 전부."""
    d = directory or trace_dir()
    stamp = day or f"{datetime.now():%Y%m%d}"     # 문자열에 날짜 서식을 붙이면 ValueError(Codex 리뷰)
    files = sorted(d.glob("*.jsonl")) if day == "all" else [d / f"{stamp}.jsonl"]
    out = []
    for f in files:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def summarize(events: list[dict]) -> dict:
    """역할별 모델 호출과 노드별 소요 시간(운영 지표, 학습 통계가 아니다)."""
    roles: dict[str, dict] = {}
    for e in (e for e in events if e.get("kind") == "llm"):
        r = roles.setdefault(e["role"], {"calls": 0, "errors": 0, "failovers": 0, "ms": [], "in": 0, "out": 0,
                                         "endpoints": {}})
        r["calls"] += 1
        r["errors"] += e["status"] != "ok"
        r["failovers"] += e.get("failovers") or 0
        r["ms"].append(e["ms"])
        r["in"] += e.get("input_tokens") or 0
        r["out"] += e.get("output_tokens") or 0
        if e.get("endpoint"):
            r["endpoints"][e["endpoint"]] = r["endpoints"].get(e["endpoint"], 0) + 1
    nodes: dict[str, dict] = {}
    for e in (e for e in events if e.get("kind") == "node"):
        n = nodes.setdefault(e["node"], {"runs": 0, "errors": 0, "interrupts": 0, "ms": []})
        n["runs"] += 1
        n["errors"] += e["status"] == "error"
        n["interrupts"] += e["status"] == "interrupt"
        n["ms"].append(e["ms"])
    for d in (*roles.values(), *nodes.values()):
        ms = d.pop("ms")
        d["p50_ms"], d["p95_ms"] = _pct(ms, 0.5), _pct(ms, 0.95)
    return {"roles": roles, "nodes": nodes,
            "traces": len({e.get("trace_id") for e in events if e.get("trace_id")})}


def recent_traces(events: list[dict], last: int = 5) -> list[tuple[str, list[dict]]]:
    """최근 trace_id 순으로 이벤트를 묶는다."""
    groups: dict[str, list[dict]] = {}
    for e in events:
        if e.get("trace_id"):
            groups.setdefault(e["trace_id"], []).append(e)
    ordered = sorted(groups.items(), key=lambda kv: kv[1][-1]["ts"])
    return ordered[-last:]


__all__ = ["GraphTracer", "TRACE_ENV", "ambient", "enabled", "load_events", "recent_traces", "record_llm", "span",
           "summarize", "trace_dir", "with_tracing", "write"]
