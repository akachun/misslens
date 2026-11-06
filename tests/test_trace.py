"""D60: 로컬 트레이스(모델 호출·그래프 노드·span), 요약, CLI. 기록은 conftest가 tmp_path로 돌린다."""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from botocore.exceptions import ClientError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

import misslens.trace as tr
from misslens.bedrock import load_model_config
from misslens.llm import LLMCallError, Router, RouterState

CFG = load_model_config()


def quota():
    return ClientError({"Error": {"Code": "ServiceQuotaExceededException", "Message": "quota"}}, "Converse")


def router(fail_first: int = 0, *, trace=True):
    """첫 fail_first개 엔드포인트는 할당량 초과, 그다음은 토큰 사용량이 있는 답을 돌려준다."""
    names = [e.name for e in CFG.endpoints]

    def factory(endpoint, model, timeout):
        if names.index(endpoint.name) < fail_first:
            class Bad:
                def invoke(self, inp, **kw):
                    raise quota()
            return Bad()
        return FakeMessagesListChatModel(responses=[AIMessage(   # 같은 답을 반복(router가 모델을 재사용)
            content="ok", usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            response_metadata={"model_name": model})])                 # Bedrock도 model_name을 넣는다

    return Router(CFG, list(CFG.endpoints), factory, state=RouterState(), sleep=lambda s: None, trace=trace)


def events(tmp_path):
    return tr.load_events(directory=tmp_path / "traces")


def test_llm_call_records_role_endpoint_failover_and_tokens(tmp_path):
    r = router(fail_first=1)
    with tr.span("generate", task_id="2.3") as sc:
        r.run("generator", lambda llm: llm.invoke("hi"))
        sc["attrs"]["tries"] = 1
    llm, sp = events(tmp_path)
    assert llm["kind"] == "llm" and llm["role"] == "generator" and llm["status"] == "ok"
    assert llm["endpoint"] == CFG.endpoints[1].name and llm["failovers"] == 1 and llm["tries"] == 2
    assert (llm["input_tokens"], llm["output_tokens"]) == (120, 30)
    assert llm["attempts"][0]["status"] == "quota_exceeded"
    assert llm["span"] == "generate" and llm["trace_id"] == sp["trace_id"]       # 같은 작업으로 묶인다
    assert sp["kind"] == "span" and sp["task_id"] == "2.3" and sp["tries"] == 1 and sp["status"] == "ok"
    assert "hi" not in json.dumps(llm)                                           # 본문은 남기지 않는다


def test_all_failed_and_untraced_router(tmp_path):
    with pytest.raises(LLMCallError):
        router(fail_first=len(CFG.endpoints)).run("verifier", lambda llm: llm.invoke("x"))
    (e,) = events(tmp_path)
    assert e["status"] == "all_failed" and e["endpoint"] is None and e["failovers"] >= 1
    router(trace=False).run("verifier", lambda llm: llm.invoke("x"))           # 테스트용 router는 기본으로 끔
    assert len(events(tmp_path)) == 1


def test_span_records_errors_and_nesting(tmp_path):
    with pytest.raises(ValueError):
        with tr.span("outer"):
            with tr.span("inner"):
                raise ValueError("boom")
    inner, outer = events(tmp_path)
    assert inner["parent"] == "outer" and inner["trace_id"] == outer["trace_id"]
    assert inner["status"] == outer["status"] == "error" and "boom" in inner["error"]


class S(TypedDict):
    x: int


def test_graph_nodes_and_llm_calls_inside_nodes(tmp_path):
    r = router()

    def work(s):
        r.run("router", lambda llm: llm.invoke("route me"))
        return {"x": s["x"] + 1}

    def fail(s):
        raise RuntimeError("node broke")

    g = StateGraph(S)
    g.add_node("work", work)
    g.add_edge(START, "work")
    g.add_edge("work", END)
    g.compile().invoke({"x": 1}, tr.with_tracing({"configurable": {"thread_id": "ui-ask-1"}}))
    llm, node = events(tmp_path)
    assert llm["node"] == "work" and llm["thread_id"] == llm["trace_id"] == "ui-ask-1"
    assert node["kind"] == "node" and node["node"] == "work" and node["status"] == "ok"
    g2 = StateGraph(S)
    g2.add_node("fail", fail)
    g2.add_edge(START, "fail")
    g2.add_edge("fail", END)
    with pytest.raises(RuntimeError):
        g2.compile().invoke({"x": 1}, tr.with_tracing({"configurable": {"thread_id": "t2"}}))
    assert events(tmp_path)[-1]["status"] == "error" and "node broke" in events(tmp_path)[-1]["error"]


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(tr.TRACE_ENV, "0")
    router().run("router", lambda llm: llm.invoke("x"))
    with tr.span("s"):
        pass
    assert events(tmp_path) == [] and tr.with_tracing({"a": 1}) == {"a": 1}


def test_summary_and_cli(tmp_path, capsys):
    from misslens.cli import main

    r = router(fail_first=1)
    for _ in range(3):
        with tr.span("generate", task_id="1.1"):
            r.run("generator", lambda llm: llm.invoke("x"))
    s = tr.summarize(events(tmp_path))
    g = s["roles"]["generator"]
    assert g["calls"] == 3 and g["errors"] == 0 and g["in"] == 360 and g["p50_ms"] is not None
    assert s["traces"] == 3
    assert main(["trace", "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "generator" in out and "span generate" in out and "task_id=1.1" in out
    tr.write({"kind": "node", "node": "planner", "status": "interrupt", "ms": 5, "trace_id": "p"},
             directory=tmp_path / "traces")
    assert tr.summarize(events(tmp_path))["nodes"]["planner"]["interrupts"] == 1


def test_cli_without_traces(tmp_path):
    from misslens.cli import main

    assert main(["trace"]) == 1


def test_load_events_accepts_a_day_string(tmp_path):
    """`trace --day 20261105`가 ValueError로 죽던 문제(Codex 리뷰)."""
    from misslens.trace import load_events

    (tmp_path / "20261105.jsonl").write_text('{"kind": "llm"}\n', encoding="utf-8")
    assert load_events("20261105", directory=tmp_path) == [{"kind": "llm"}]
