"""Supervisor 그래프: 모든 요청을 에이전트로 보내는 하나의 LangGraph(ARCHITECTURE §2, D1).

    START -> route -+-> planner (subgraph, interrupt①) -> generate -> END
                    +-> generate ---------------------------------> END      quiz(명령 또는 자유 입력)
                    +-> report -----------------------------------> END      status(Reporter)
                    +-> collect_prepare -> collect_<site> (subgraph, interrupt②) -> ingest_collected -> END
                    +-> ingest ------------------------------------> END     파일(Ingest)
                    +-> submit ------------------------------------> END     답안(Grader)
                    +-> resume ------------------------------------> END     멈춘 세트 이어 가기(D52)
                    +-> END                                                   불분명한 자유 입력: 안내 메시지

interrupt는 planner(계획 승인)와 collector(로그인 대기) subgraph 안에만 있다(CLAUDE.md rule 4).
문제 풀이와 리뷰 양식은 평범한 UI 단계이고, 그 결과는 `submit` 요청으로 돌아온다.

checkpoint되는 state에 둘 수 없는 객체(실행 중인 문제 스트림, 브라우저 fetcher)는 `Services`에 두고,
state에는 id와 일반 데이터만 담는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from misslens.agents.collector.graph import build_collector_graph, initial_state
from misslens.agents.collector.sources import SourceList
from misslens.agents.generator import Generator, QuestionStream
from misslens.agents.planner import build_planner_graph
from misslens.agents.reporter import build_report
from misslens.agents.supervisor import DEFAULT_COUNT, INTENTS, route_text
from misslens.exam_pack import ExamPack
from misslens.ledger import Answer, Ledger, set_score

logger = logging.getLogger(__name__)


@dataclass
class Services:
    pack: ExamPack
    ledger: Ledger
    make_generator: Callable[[], Generator]
    router_llm: Any = None
    planner_llm: Any = None
    make_ingestor: Callable[[], Any] | None = None
    sources: SourceList | None = None
    fetchers: dict[str, Any] = field(default_factory=dict)       # site -> Fetcher
    streams: dict[int, QuestionStream] = field(default_factory=dict)
    index: Any = None                                             # 코치의 검색 도구가 쓰는 ChunkIndex


class SupervisorState(TypedDict, total=False):
    # 요청
    text: str | None
    intent: str | None
    args: dict
    # planner subgraph 키
    total: int
    plan: dict | None
    plan_table: list[dict]
    plan_status: str | None
    plan_error: str | None
    # collector subgraph 키
    site: str
    queue: list[dict]
    seen: list[str]
    results: list[dict]
    need_login: bool
    login_waits: int
    stopped: str | None
    # 출력
    set_id: int | None
    report: dict | None
    ingested: list[dict] | None
    score: dict | None
    message: str | None


def build_supervisor_graph(svc: Services, *, checkpointer: Any = None):
    pack, ledger = svc.pack, svc.ledger

    def route(state: SupervisorState) -> SupervisorState:
        args = dict(state.get("args") or {})
        intent = state.get("intent")
        if intent is None:
            if not state.get("text") or svc.router_llm is None:
                return {"intent": "end", "message": "요청이 비어 있습니다."}
            intent, extra, msg = route_text(pack, state["text"], svc.router_llm)
            args.update(extra)
            if intent == "end":
                return {"intent": "end", "message": msg, "args": args}
        if intent not in INTENTS:
            return {"intent": "end", "message": f"알 수 없는 요청입니다: {intent}"}
        if intent in ("collect", "ingest"):
            from misslens.paths import SandboxRefused, refuse_in_sandbox

            try:
                refuse_in_sandbox("교안 수집" if intent == "collect" else "자료 색인")
            except SandboxRefused as exc:
                return {"intent": "end", "message": str(exc)}
        update: SupervisorState = {"intent": intent, "args": args}
        if intent == "plan_quiz":
            update["total"] = int(args.get("count") or DEFAULT_COUNT)
        return update

    def generate(state: SupervisorState, config: RunnableConfig) -> SupervisorState:
        args = state.get("args") or {}
        if state.get("intent") == "plan_quiz":
            if state.get("plan_status") != "approved":
                return {"message": "계획을 취소했습니다. 세트를 만들지 않았습니다."}
            plan = state["plan"]["counts"]
        elif args.get("domain"):
            plan = {args["domain"]: int(args.get("count") or DEFAULT_COUNT)}
        else:
            plan = None
        n = sum(plan.values()) if plan else int(args.get("count") or DEFAULT_COUNT)
        set_id = ledger.create_set(pack.exam_id, n)
        approved = state.get("plan") if state.get("intent") == "plan_quiz" else None
        ledger.record_plan(pack.exam_id, set_id=set_id, kind="plan" if approved else "quick", total=n, counts=plan,
                           rationale=(approved or {}).get("rationale", ""), source=(approved or {}).get("source", ""),
                           thread_id=(config.get("configurable") or {}).get("thread_id"))
        stream = QuestionStream(svc.make_generator(), set_id, n, plan=plan).start()
        svc.streams[set_id] = stream
        return {"set_id": set_id, "message": f"{n}문제 세트 #{set_id}를 만드는 중입니다."}

    def resume(state: SupervisorState) -> SupervisorState:
        """생성이 멈춘 세트(예: 서버 재시작)를 이어 간다. 빠진 문제만 만든다(D52)."""
        set_id = int((state.get("args") or {})["set_id"])
        prog = ledger.set_progress(set_id)
        if prog["status"] not in ("generating", "in_progress"):
            return {"set_id": set_id, "message": f"세트 #{set_id}는 이미 제출됐습니다."}
        live = svc.streams.get(set_id)
        if (live is not None and live.running) or prog["ready"] >= prog["size"]:
            return {"set_id": set_id, "message": f"세트 #{set_id}를 이어서 풉니다."}
        plan = (ledger.plan_for_set(set_id) or {}).get("counts")
        svc.streams[set_id] = QuestionStream(svc.make_generator(), set_id, prog["size"], plan=plan).start()
        return {"set_id": set_id,
                "message": f"세트 #{set_id}의 남은 {prog['size'] - prog['ready']}문제를 다시 만드는 중입니다."}

    def report(state: SupervisorState) -> SupervisorState:
        return {"report": build_report(pack, ledger).as_dict()}

    def submit(state: SupervisorState) -> SupervisorState:
        args = state.get("args") or {}
        set_id = int(args["set_id"])
        ledger.submit_set(set_id, [Answer(**a) for a in args["answers"]])
        s = set_score(ledger, set_id)
        return {"set_id": set_id, "score": {"total": s.total, "correct": s.correct}}

    def ingest(state: SupervisorState) -> SupervisorState:
        from misslens.agents.ingest import UnsupportedFile, load_file

        if svc.make_ingestor is None:
            return {"message": "자료 색인을 쓸 수 없습니다."}
        from misslens.agents.ingest import from_paste

        ing, out = svc.make_ingestor(), []
        for item in (state.get("args") or {}).get("paste", []):
            r = ing.ingest(from_paste(item["text"], item.get("label") or "paste"))
            out.append({"source": r.source, "status": r.status, "chunks": r.chunks})
        for p in (state.get("args") or {}).get("paths", []):
            try:
                r = ing.ingest(load_file(p))
                out.append({"source": r.source, "status": r.status, "chunks": r.chunks})
            except (UnsupportedFile, OSError) as exc:
                out.append({"source": str(p), "status": "skipped", "chunks": 0, "note": str(exc)})
        return {"ingested": out}

    def collect_prepare(state: SupervisorState) -> SupervisorState:
        if svc.sources is None or not svc.sources.sites:
            return {"site": "", "queue": [], "message": "sources.yaml에 수집 대상이 없습니다."}
        site = (state.get("args") or {}).get("site") or next(iter(svc.sources.sites))
        if site not in svc.fetchers:
            return {"site": "", "queue": [], "message": f"수집할 수 없는 사이트: {site}"}
        return dict(initial_state(svc.sources.sites[site], svc.sources.for_site(site)))

    def ingest_collected(state: SupervisorState) -> SupervisorState:
        from misslens.agents.ingest import load_file

        fetcher = svc.fetchers.get(state.get("site", ""))
        if fetcher is not None:
            fetcher.close()
        saved = [r["path"] for r in state.get("results", []) if r.get("status") == "saved"]
        if not saved or svc.make_ingestor is None:
            return {"ingested": []}
        ing, out = svc.make_ingestor(), []
        for p in saved:
            r = ing.ingest(dc_replace(load_file(p), source_type="collect"))
            out.append({"source": r.source, "status": r.status, "chunks": r.chunks})
        return {"ingested": out}

    g = StateGraph(SupervisorState)
    g.add_node("route", route)
    g.add_node("planner", build_planner_graph(pack, ledger, svc.planner_llm))
    g.add_node("generate", generate)
    g.add_node("resume", resume)
    g.add_node("report", report)
    g.add_node("submit", submit)
    g.add_node("ingest", ingest)
    g.add_node("collect_prepare", collect_prepare)
    g.add_node("ingest_collected", ingest_collected)
    collect_nodes = []
    for name, fetcher in svc.fetchers.items():
        if svc.sources is not None and name in svc.sources.sites:
            node = f"collect_{name}"
            g.add_node(node, build_collector_graph(svc.sources.sites[name], fetcher))
            g.add_edge(node, "ingest_collected")
            collect_nodes.append(node)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", lambda s: {
        "plan_quiz": "planner", "quiz": "generate", "status": "report", "collect": "collect_prepare",
        "ingest": "ingest", "submit": "submit", "resume": "resume"}.get(s.get("intent"), END),
        ["planner", "generate", "report", "collect_prepare", "ingest", "submit", "resume", END])
    g.add_edge("planner", "generate")
    g.add_conditional_edges("collect_prepare",
                            lambda s: f"collect_{s['site']}" if s.get("queue") else END, collect_nodes + [END])
    for n in ("generate", "resume", "report", "submit", "ingest", "ingest_collected"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)


__all__ = ["Services", "SupervisorState", "build_supervisor_graph"]
