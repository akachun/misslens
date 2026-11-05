"""Streamlit UI: 학습 · 자료 · 현황 · 계획 tabs over the Supervisor graph (SERVICE §5, ARCHITECTURE §10, T9).

Every agent request goes through the Supervisor graph and runs as a background *job* keyed by its thread_id, so
long work (collection, generation start) never freezes the page and an interrupt (plan approval, login wait) is
read back from the checkpointer. The plan thread_id and the active set id are mirrored into the URL
(`?plan=<thread>&set=<id>`), so a refresh resumes the approval screen or the set in progress.

Solving and the review form are plain widgets (no interrupts, CLAUDE.md rule 4). Questions shown come from the
ledger (status 'ready' only: they passed the validator and grounding check, rule 2).
"""

from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import streamlit as st
from langgraph.types import Command

from misslens.agents.grader import build_reask_form, diagnosis_rows, highlight, submit_review
from misslens.agents.grader.coach import coach_ask, coach_explain, coach_history, coach_thread
from misslens.bedrock import PROJECT_ROOT
from misslens.ledger import miss_breakdown

LETTERS = "ABCD"
POLL_SECONDS = 1.5
UPLOAD_DIR = PROJECT_ROOT / "data" / "sources" / "uploads"
LABELS = {"reading": "해석 실수", "uncertain": "불확실", "concept": "개념 실수"}


# --- resources (one per process) -------------------------------------------------------------

@dataclass
class Resources:
    pack: Any
    ledger: Any
    svc: Any                      # misslens.graph.Services
    graph: Any                    # compiled Supervisor graph with a checkpointer
    explainer: Any = None
    index_stats: Callable[[], dict] = lambda: {}
    upload_dir: Path = UPLOAD_DIR
    jobs: dict = field(default_factory=dict)   # thread_id -> {"thread", "out", "error"}
    coach_checkpointer: Any = None             # keeps each miss's coach conversation (None: in-memory)

    def __post_init__(self) -> None:
        if self.coach_checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver

            self.coach_checkpointer = InMemorySaver()


def default_resources() -> Resources:
    import os

    from misslens.agents.collector import sqlite_checkpointer
    from misslens.cli import make_services
    from misslens.exam_pack import load_exam_pack
    from misslens.graph import build_supervisor_graph
    from misslens.ledger import Ledger
    from misslens.llm import get_llm

    pack = load_exam_pack(os.environ.get("MISSLENS_EXAM", "ccar-f"))
    ledger = Ledger()
    svc = make_services(pack, ledger)

    def index_stats() -> dict:
        from misslens.cli import open_index

        idx = open_index(pack.exam_id)
        return idx.tag_counts() if idx is not None else {}

    return Resources(pack, ledger, svc, build_supervisor_graph(svc, checkpointer=sqlite_checkpointer()),
                     explainer=get_llm("explainer"), index_stats=index_stats,
                     coach_checkpointer=sqlite_checkpointer())


RESOURCE_FACTORY: Callable[[], Resources] | None = None   # tests replace this


@st.cache_resource
def resources() -> Resources:
    return (RESOURCE_FACTORY or default_resources)()


# --- supervisor jobs ---------------------------------------------------------------------------

def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 2000}


def new_thread(kind: str) -> str:
    return f"ui-{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"


def start_job(r: Resources, thread_id: str, payload: Any) -> None:
    job = {"thread": None, "out": None, "error": None}

    def work() -> None:
        try:
            job["out"] = r.graph.invoke(payload, run_config(thread_id))
        except Exception as exc:  # noqa: BLE001 - shown to the learner, never swallowed
            job["error"] = f"{type(exc).__name__}: {exc}"

    job["thread"] = threading.Thread(target=work, name=f"job-{thread_id}", daemon=True)
    r.jobs[thread_id] = job
    job["thread"].start()


def job_state(r: Resources, thread_id: str) -> tuple[str, Any]:
    """-> ("running", None) | ("error", msg) | ("interrupt", payload) | ("done", values) | ("none", None).
    Falls back to the checkpointer when the job is not in memory (page refresh after a restart)."""
    job = r.jobs.get(thread_id)
    if job is not None:
        if job["thread"].is_alive():
            return "running", None
        if job["error"]:
            return "error", job["error"]
    snap = r.graph.get_state(run_config(thread_id))
    pending = [i for t in snap.tasks for i in t.interrupts]
    if pending:
        return "interrupt", pending[0].value
    if snap.values:
        return "done", snap.values
    return "none", None


def wait_rerun() -> None:
    """Ask for a rerun after every tab has rendered (a long job in one tab must not hide the others)."""
    st.session_state["_poll"] = True


# --- quiz session ------------------------------------------------------------------------------

def start_quiz(set_id: int, n: int) -> None:
    st.session_state.quiz = {"set_id": set_id, "n": n, "idx": 0, "answers": {}, "shown": {}, "phase": "solving",
                             "notes": [], "items": None}
    st.query_params["set"] = str(set_id)


def adopt_result(values: dict, thread_id: str) -> None:
    """Handle a finished Supervisor run once: a new set starts the quiz."""
    done = st.session_state.setdefault("adopted", set())
    if thread_id in done:
        return
    done.add(thread_id)
    sid = values.get("set_id")
    if sid is not None and values.get("score") is None:
        plan = values.get("plan") if values.get("plan_status") == "approved" else None
        n = sum(plan["counts"].values()) if plan else int((values.get("args") or {}).get("count") or 10)
        start_quiz(int(sid), n)


def ready_questions(r: Resources, set_id: int) -> list[dict]:
    return [q for q in r.ledger.set_questions(set_id) if q["status"] == "ready"]


def tab_study(r: Resources) -> None:
    quiz = st.session_state.get("quiz")
    if quiz is None and st.query_params.get("set"):
        sid = int(st.query_params["set"])
        status = r.ledger.get_set(sid)["status"]
        start_quiz(sid, len(ready_questions(r, sid)) or 1)
        quiz = st.session_state.quiz
        quiz["phase"] = {"submitted": "review", "reviewed": "diagnosis"}.get(status, "solving")
    if quiz is None:
        study_start(r)
        return
    {"solving": study_solve, "review": study_review, "self_check": study_self_check,
     "diagnosis": study_diagnosis}[quiz["phase"]](r, quiz)


def study_start(r: Resources) -> None:
    st.subheader("바로 풀기")
    c1, c2 = st.columns(2)
    n = c1.number_input("문제 수", 1, 30, 5, key="quick_n")
    domains = ["전체(범위 먼저, 그다음 약점)"] + [f"{d.id} {d.name_native or d.name}" for d in r.pack.domains]
    pick = c2.selectbox("영역", domains, key="quick_domain")
    if st.button("세트 시작", key="quick_start", type="primary"):
        args = {"count": int(n)}
        if pick != domains[0]:
            args["domain"] = pick.split()[0]
        tid = new_thread("quiz")
        st.session_state.quiz_thread = tid
        start_job(r, tid, {"intent": "quiz", "args": args})
    tid = st.session_state.get("quiz_thread")
    if tid:
        state, val = job_state(r, tid)
        if state == "running":
            st.info("세트를 준비하는 중...")
            wait_rerun()
        elif state == "error":
            st.error(val)
        elif state == "done":
            adopt_result(val, tid)
            st.rerun()
    st.caption("계획을 세우고 싶으면 '계획' 탭, 자연어로 요청하려면 왼쪽 입력창을 쓰세요.")


def study_solve(r: Resources, quiz: dict) -> None:
    sid = quiz["set_id"]
    stream = r.svc.streams.get(sid)
    if stream is not None:
        for ev in stream.poll():
            if ev.kind == "failed":
                quiz["notes"].append(f"문제 {ev.seq + 1} 생성 실패: {ev.message}")
            elif ev.kind in ("done", "stopped"):
                quiz["notes"].append(ev.message)
    generating = stream is not None and stream.running
    ready = ready_questions(r, sid)
    st.subheader(f"세트 #{sid} · {len(ready)}/{quiz['n']}문제 준비")
    for note in quiz["notes"]:
        st.caption(note)
    if quiz["idx"] < len(ready):
        if not quiz.get("started"):
            # First question on screen: generating -> in_progress, as the CLI does (§4.1).
            if r.ledger.get_set(sid)["status"] == "generating":
                r.ledger.start_set(sid)
            quiz["started"] = True
        q = ready[quiz["idx"]]
        p = q["payload"]
        quiz["shown"].setdefault(q["id"], time.monotonic())
        st.markdown(f"**문제 {q['seq'] + 1}** · `{q['task_id']}`")
        st.write(p["stem_en"])
        choice = st.radio("답", list(range(4)), index=None, key=f"ans_{q['id']}",
                          format_func=lambda i, o=p["options"]: f"{LETTERS[i]}. {o[i]['text_en']}")
        if st.button("다음", key=f"next_{q['id']}", type="primary"):
            quiz["answers"][q["id"]] = {"question_id": q["id"], "chosen_index": choice,
                                        "elapsed_ms": int((time.monotonic() - quiz["shown"][q["id"]]) * 1000)}
            quiz["idx"] += 1
            st.rerun()
        return
    if generating:
        st.info("다음 문제를 생성하는 중...")
        wait_rerun()
        return
    if not ready:
        st.warning("풀 문제가 없습니다. 현황 탭에서 생성 실패 항목을 확인하세요.")
        if st.button("처음으로", key="reset_empty"):
            reset_quiz()
        return
    st.success(f"{len(ready)}문제를 모두 풀었습니다.")
    if st.button("세트 제출", key="submit_set", type="primary"):
        answers = [quiz["answers"].get(q["id"], {"question_id": q["id"], "chosen_index": None, "elapsed_ms": None})
                   for q in ready]
        out = r.graph.invoke({"intent": "submit", "args": {"set_id": sid, "answers": answers}},
                             run_config(new_thread("submit")))
        quiz["score"] = out["score"]
        quiz["phase"] = "review"
        st.rerun()


def study_review(r: Resources, quiz: dict) -> None:
    sid = quiz["set_id"]
    score = quiz.get("score")
    if score:
        st.success(f"제출 완료: {score['total']}문제 중 {score['correct']}문제 정답")
    if quiz["items"] is None:
        quiz["items"] = (build_reask_form(r.ledger.set_questions(sid), r.ledger.set_attempts(sid))
                         if r.pack.needs_reask else [])
    items = quiz["items"]
    if items:
        st.subheader("리뷰: 한국어로 다시 풀기")
        st.caption("맞힌 문제도 섞여 있습니다. 답과 함께 확신 여부를 고르세요.")
    with st.form(f"reask_{sid}"):
        for i, it in enumerate(items):
            st.markdown(f"**다시 풀기 {i + 1}/{len(items)}**")
            st.write(it.stem)
            st.radio("답", list(range(len(it.options))), index=None, key=f"re_{sid}_{i}",
                     format_func=lambda j, o=it.options: f"{LETTERS[j]}. {o[j]}")
            st.checkbox("확신합니다(체크 안 하면 추측)", key=f"conf_{sid}_{i}")
        sent = st.form_submit_button("리뷰 제출" if items else "진단 보기", type="primary")
    if sent:
        responses = [(st.session_state.get(f"re_{sid}_{i}"), bool(st.session_state.get(f"conf_{sid}_{i}")))
                     for i in range(len(items))]
        if any(c is None for c, _ in responses):
            st.error("모든 문제에 답을 고르세요.")
            return
        submit_review(r.ledger, r.pack, sid, items, responses)
        misses = [x for x in diagnosis_rows(r.ledger, sid) if not x.is_correct and x.miss_type]
        quiz["phase"] = "self_check" if misses else "diagnosis"
        st.rerun()


SELF_LABELS = {"concept": "몰랐던 내용(개념)", "reading": "알았는데 조건을 놓침(해석)", "unsure": "모르겠음"}


def study_self_check(r: Resources, quiz: dict) -> None:
    """Learner's own label per miss: answer shown, verdict and explanation not yet (D43, eval data)."""
    sid = quiz["set_id"]
    misses = [x for x in diagnosis_rows(r.ledger, sid) if not x.is_correct and x.miss_type]
    st.subheader("자기 판단")
    st.caption("틀린 문제의 정답을 먼저 봅니다. 해설을 보기 전에 왜 틀렸다고 생각하는지 고르세요. "
               "진단이 맞는지 확인하는 데만 쓰고 판정은 바꾸지 않습니다. 건너뛰어도 됩니다.")
    for row in misses:
        q = row.question
        chosen = "-" if row.chosen_index is None else f"{LETTERS[row.chosen_index]}. {q['options'][row.chosen_index]['text_en']}"
        st.markdown(f"**문제 {row.seq + 1}**")
        st.write(q["stem_en"])
        st.markdown(f"고른 답: {chosen}  \n정답: **{LETTERS[q['answer_index']]}. {q['options'][q['answer_index']]['text_en']}**")
        st.radio("정답을 보니", list(SELF_LABELS), index=None, horizontal=True, key=f"self_{row.attempt_id}",
                 format_func=SELF_LABELS.get)
    if st.button("진단 보기", key=f"self_done_{sid}", type="primary"):
        for row in misses:
            label = st.session_state.get(f"self_{row.attempt_id}")
            if label:
                r.ledger.record_miss_feedback(row.attempt_id, label)
        quiz["phase"] = "diagnosis"
        st.rerun()


def study_diagnosis(r: Resources, quiz: dict) -> None:
    sid = quiz["set_id"]
    rows = diagnosis_rows(r.ledger, sid)
    mb = miss_breakdown(r.ledger, r.pack.exam_id, set_id=sid)
    c = st.columns(4)
    c[0].metric("개념 실수", mb.concept)
    c[1].metric("해석 실수", mb.reading)
    c[2].metric("불확실", mb.uncertain)
    c[3].metric("미끼 기준선", "-" if mb.baseline is None else f"{mb.baseline:.0%}")
    invalid = {a["id"] for a in r.ledger.set_attempts(sid) if a["invalid"]}
    for row in rows:
        q = row.question
        if row.is_correct:
            with st.expander(f"✅ 문제 {row.seq + 1} 정답" + (" (미끼)" if row.is_decoy else "")
                             + f" · 정답 {LETTERS[q['answer_index']]}", expanded=False):
                st.write(q["stem_en"])
                for i, o in enumerate(q["options"]):
                    mark = "**" if i == q["answer_index"] else ""
                    st.markdown(f"{mark}{LETTERS[i]}. {o['text_en']}{mark}")
                st.markdown("**해설**")
                st.write(q["explanation_native"])
                coach_panel(r, row)
            continue
        chosen = "-" if row.chosen_index is None else LETTERS[row.chosen_index]
        title = (f"문제 {row.seq + 1} · {LABELS.get(row.miss_type, row.miss_type or '-')} · "
                 f"고른 답 {chosen} → 정답 {LETTERS[q['answer_index']]}"
                 + (" · 통계 제외됨" if row.attempt_id in invalid else ""))
        with st.expander(title, expanded=row.attempt_id not in invalid):
            stem = highlight(html.escape(q["stem_en"]), html.escape(row.missed_phrase) if row.missed_phrase else None,
                             "<mark>", "</mark>")
            st.markdown(stem, unsafe_allow_html=True)
            if row.missed_phrase:
                st.markdown(f"**놓친 조건:** <mark>{html.escape(row.missed_phrase)}</mark>", unsafe_allow_html=True)
            st.markdown("**해설**")
            st.write(q["explanation_native"])
            coach_panel(r, row)
            if row.attempt_id not in invalid and st.button("문제 이상(통계에서 빼기)", key=f"inv_{row.attempt_id}"):
                r.ledger.mark_invalid(row.attempt_id)
                st.rerun()
    if st.button("새 세트", key="new_set", type="primary"):
        reset_quiz()


def source_links(sources: list[str]) -> str:
    """Markdown: URLs become links the learner can open (Skilljar lessons open in their logged-in browser)."""
    parts = []
    for s in sources:
        if s.startswith(("http://", "https://")):
            label = "/".join(s.rstrip("/").split("/")[-2:])
            parts.append(f"[{label}]({s})")
        else:
            parts.append(f"`{s}`")
    return " · ".join(parts)


def show_turn_meta(sources: list[str], tool_calls: list[str]) -> None:
    if sources:
        st.markdown("근거: " + source_links(sources))
    if tool_calls:
        st.caption("코치가 쓴 도구: " + " → ".join(tool_calls))


def coach_panel(r: Resources, row: Any) -> None:
    """Coach explanation below the stored one, then a follow-up conversation on the same miss (D47)."""
    if r.explainer is None:
        return
    tid = coach_thread(r.pack.exam_id, row.attempt_id)
    turns = coach_history(r.coach_checkpointer, tid)
    fallback = st.session_state.get(f"coach_fallback_{row.attempt_id}")
    if not turns:
        if fallback is not None:
            st.markdown("**코치 해설**")
            st.write(fallback)
            st.caption("코치가 자료 검색 없이 답했습니다(모델 호출 실패). 아래에서 질문하면 다시 시도합니다.")
        elif not row.is_correct and st.button("코치 해설(자료 검색)", key=f"btn_exp_{row.attempt_id}"):
            with st.spinner("코치가 자료를 찾아 해설을 쓰는 중..."):
                res = coach_explain(r.explainer, r.pack, row, ledger=r.ledger, index=r.svc.index,
                                    checkpointer=r.coach_checkpointer, thread_id=tid)
            if not coach_history(r.coach_checkpointer, tid):     # coach failed: plain explanation, no thread
                st.session_state[f"coach_fallback_{row.attempt_id}"] = res.text
            st.rerun()
    elif turns[0].question is None:                              # started with the coach explanation
        st.markdown("**코치 해설**")
        st.write(turns[0].answer)
        show_turn_meta(turns[0].sources, turns[0].tool_calls)
    for t in (turns[1:] if turns and turns[0].question is None else turns):
        with st.chat_message("user"):
            st.write(t.question)
        with st.chat_message("assistant"):
            st.write(t.answer)
            show_turn_meta(t.sources, t.tool_calls)
    with st.form(f"ask_{row.attempt_id}", clear_on_submit=True):
        question = st.text_input("코치에게 질문", key=f"q_{row.attempt_id}",
                                 placeholder="예: tool_choice any와 auto는 언제 다르게 쓰나요?")
        if st.form_submit_button("질문하기") and question.strip():
            with st.spinner("코치가 답을 찾는 중..."):
                coach_ask(r.explainer, r.pack, question, ledger=r.ledger, index=r.svc.index,
                          checkpointer=r.coach_checkpointer, thread_id=tid, row=row)
            st.rerun()


def reset_quiz() -> None:
    st.session_state.pop("quiz", None)
    st.session_state.pop("quiz_thread", None)
    if "set" in st.query_params:
        del st.query_params["set"]
    st.rerun()


# --- plan tab (interrupt①) ---------------------------------------------------------------------

def tab_plan(r: Resources) -> None:
    tid = st.session_state.get("plan_thread") or st.query_params.get("plan")
    if not tid:
        n = st.number_input("오늘 풀 문제 수", 1, 30, 10, key="plan_n")
        if st.button("계획 만들기", key="plan_make", type="primary"):
            tid = new_thread("plan")
            st.session_state.plan_thread = tid
            st.query_params["plan"] = tid
            start_job(r, tid, {"intent": "plan_quiz", "args": {"count": int(n)}})
            st.rerun()
        return
    state, val = job_state(r, tid)
    if state == "running":
        st.info("계획을 세우는 중...")
        wait_rerun()
    elif state == "error":
        st.error(val)
        clear_plan_button()
    elif state == "interrupt" and val.get("kind") == "plan":
        plan_approval(r, tid, val)
    elif state == "done":
        if val.get("plan_status") == "approved" and val.get("set_id") is not None:
            adopt_result(val, tid)
            st.success(f"계획대로 세트 #{val['set_id']}를 만들고 있습니다. '학습' 탭에서 푸세요.")
        else:
            st.info(val.get("message") or "계획을 취소했습니다.")
        clear_plan_button()
    else:
        clear_plan_button()


def plan_approval(r: Resources, tid: str, payload: dict) -> None:
    plan = payload["plan"]
    st.subheader(f"오늘의 계획 · {plan['total']}문제")
    st.write(plan.get("rationale", ""))
    if payload.get("error"):
        st.error(payload["error"])
    table = {d["domain"]: d for d in payload.get("table", [])}
    counts = {}
    for dom, n in plan["counts"].items():
        d = table.get(dom, {})
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{dom} {d.get('name', '')}** · 비중 {d.get('weight', 0):.0%} · 출제 항목 "
                    f"{d.get('asked', 0)}/{d.get('tasks', 0)} · 정답률 {d.get('accuracy', 0.5):.0%}")
        counts[dom] = c2.number_input("문제 수", 0, 30, int(n), key=f"plan_{tid}_{dom}", label_visibility="collapsed")
    b1, b2 = st.columns(2)
    if b1.button("승인하고 세트 만들기", key=f"approve_{tid}", type="primary"):
        start_job(r, tid, Command(resume={"action": "approve", "counts": counts}))
        st.rerun()
    if b2.button("취소", key=f"cancel_{tid}"):
        start_job(r, tid, Command(resume={"action": "cancel"}))
        st.rerun()
    st.caption("이 화면은 새로고침해도 그대로 이어집니다(주소의 plan 값).")


def clear_plan_button() -> None:
    if st.button("새 계획", key="plan_reset"):
        st.session_state.pop("plan_thread", None)
        if "plan" in st.query_params:
            del st.query_params["plan"]
        st.rerun()


# --- material tab ------------------------------------------------------------------------------

def tab_material(r: Resources) -> None:
    from misslens.paths import sandbox

    if sandbox():
        st.info("샌드박스 모드에서는 자료 넣기와 수집을 할 수 없습니다(색인·수집본은 실제 데이터와 공유).")
        material_index_stats(r)
        return
    st.subheader("자료 넣기")
    files = st.file_uploader("PDF / MD / TXT / HTML", accept_multiple_files=True, key="uploads",
                             type=["pdf", "md", "markdown", "txt", "html", "htm"])
    label = st.text_input("붙여넣기 자료 이름", key="paste_label", value="붙여넣기")
    text = st.text_area("또는 텍스트 붙여넣기", key="paste_text", height=120)
    if st.button("색인에 넣기", key="ingest_go", type="primary") and (files or text.strip()):
        r.upload_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files or []:
            p = r.upload_dir / Path(f.name).name
            p.write_bytes(f.getvalue())
            paths.append(str(p))
        args: dict = {"paths": paths}
        if text.strip():
            args["paste"] = [{"label": label, "text": text}]
        tid = new_thread("ingest")
        st.session_state.ingest_thread = tid
        start_job(r, tid, {"intent": "ingest", "args": args})
    show_job_result(r, "ingest_thread", "자료를 나누고 태깅하는 중...")

    st.subheader("교안 수집(로그인 세션)")
    sites = list(r.svc.fetchers)
    if not sites:
        st.caption("sources.yaml에 수집 대상이 없습니다.")
    else:
        site = st.selectbox("사이트", sites, key="collect_site")
        st.caption(f"로그인은 이 앱이 도는 컴퓨터에서 `python -m misslens.cli collect login {site}`로 직접 합니다.")
        if st.button("수집 시작", key="collect_go"):
            tid = new_thread("collect")
            st.session_state.collect_thread = tid
            start_job(r, tid, {"intent": "collect", "args": {"site": site}})
        tid = st.session_state.get("collect_thread")
        if tid:
            state, val = job_state(r, tid)
            if state == "interrupt" and val.get("kind") == "login":
                st.warning(f"로그인이 필요합니다(멈춘 페이지: {val['url']}). 이 컴퓨터에서 "
                           f"`python -m misslens.cli collect login {val['site']}`로 로그인한 뒤 '완료'를 누르세요.")
                b1, b2 = st.columns(2)
                if b1.button("완료(이어서 수집)", key=f"login_done_{tid}", type="primary"):
                    start_job(r, tid, Command(resume="done"))
                    st.rerun()
                if b2.button("수집 중단", key=f"login_abort_{tid}"):
                    start_job(r, tid, Command(resume="abort"))
                    st.rerun()
            else:
                show_job_result(r, "collect_thread", "수집하는 중... (페이지마다 2초 이상 간격)")

    material_index_stats(r)


def material_index_stats(r: Resources) -> None:
    st.subheader("색인 현황")
    counts = r.index_stats()
    if counts:
        missing = [t for t in r.pack.task_ids if t not in counts]
        st.write(f"세부 항목 {len(r.pack.task_ids) - len(missing)}/{len(r.pack.task_ids)}개에 자료가 있습니다 · "
                 f"미분류 청크 {counts.get('unassigned', 0)}개")
        if missing:
            st.caption("자료 없는 세부 항목: " + ", ".join(missing))
    else:
        st.caption("아직 색인이 없습니다.")


def show_job_result(r: Resources, key: str, running_msg: str) -> None:
    tid = st.session_state.get(key)
    if not tid:
        return
    state, val = job_state(r, tid)
    if state == "running":
        st.info(running_msg)
        wait_rerun()
    elif state == "error":
        st.error(val)
    elif state == "done":
        if val.get("message"):
            st.info(val["message"])
        for x in val.get("results") or []:
            if x["status"] != "saved":
                st.caption(f"{x['status']} · {x['url']} {x.get('note') or ''}")
        rows = val.get("ingested") or []
        if rows:
            st.dataframe(rows, hide_index=True)
        if val.get("stopped"):
            st.warning(val["stopped"])


# --- status tab ---------------------------------------------------------------------------------

def tab_status(r: Resources) -> None:
    from misslens.agents.reporter import build_report

    rep = build_report(r.pack, r.ledger).as_dict()
    render_report(rep)


def render_report(rep: dict) -> None:
    c = st.columns(4)
    c[0].metric("범위", f"{len(rep['asked'])}/{rep['tasks_total']}", f"{rep['coverage_ratio']:.0%}")
    c[1].metric("해석 실수 비율", "-" if rep["reading_ratio"] is None else f"{rep['reading_ratio']:.0%}")
    c[2].metric("미끼 기준선", "-" if rep["baseline"] is None else f"{rep['baseline']:.0%}")
    c[3].metric("평균 풀이 시간", "-" if rep["avg_time_ms"] is None else f"{rep['avg_time_ms'] / 1000:.0f}초")
    st.caption(f"개념 {rep['concept']} · 해석 {rep['reading']} · 불확실 {rep['uncertain']} "
               "(해석 실수 비율과 미끼 기준선은 나란히 봅니다)")
    st.dataframe([{"영역": f"{d['domain']} {d['name']}", "비중": f"{d['weight']:.0%}",
                   "출제 항목": f"{d['asked']}/{d['tasks']}", "풀이": d["attempts"],
                   "정답률(보정)": f"{d['accuracy']:.0%}"} for d in rep["domains"]], hide_index=True)
    if rep["top_phrases"]:
        st.markdown("**놓친 조건 문구 상위 10**")
        st.dataframe([{"문구": p, "횟수": n} for p, n in rep["top_phrases"]], hide_index=True)
    ag = rep.get("agreement") or {}
    if ag.get("labeled"):
        rate = "-" if ag["rate"] is None else f"{ag['rate']:.0%}"
        st.caption(f"진단 일치율(내 판단과) {rate} · {ag['agree']}/{ag['compared']} · 판단 남긴 오답 {ag['labeled']}개 "
                   "(`python -m misslens.cli eval --export`로 eval 데이터 저장)")
    if rep["never_asked"]:
        st.caption("미출제 세부 항목: " + ", ".join(rep["never_asked"]))
    for f in rep["failures"]:
        st.caption(f"생성 실패 {f['task_id']} ({f['tries']}회): {f['reason'][:160]}")


# --- sidebar: natural-language request (F10) ----------------------------------------------------

def sidebar_request(r: Resources) -> None:
    with st.sidebar:
        st.markdown("### MissLens에게 요청")
        text = st.text_input("예: 도구 설계 영역 5문제", key="nl_text")
        if st.button("보내기", key="nl_send") and text.strip():
            tid = new_thread("ask")
            st.session_state.nl_thread = tid
            start_job(r, tid, {"text": text.strip()})
        tid = st.session_state.get("nl_thread")
        if not tid:
            return
        state, val = job_state(r, tid)
        if state == "running":
            st.info("처리 중...")
            wait_rerun()
        elif state == "error":
            st.error(val)
        elif state == "interrupt" and val.get("kind") == "plan":
            st.session_state.plan_thread = tid
            st.query_params["plan"] = tid
            st.success("계획을 만들었습니다. '계획' 탭에서 승인하세요.")
        elif state == "interrupt" and val.get("kind") == "login":
            st.session_state.collect_thread = tid
            st.warning("로그인이 필요합니다. '자료' 탭을 보세요.")
        elif state == "done":
            if val.get("message"):
                st.info(val["message"])
            if val.get("set_id") is not None:
                adopt_result(val, tid)
                st.success(f"세트 #{val['set_id']}를 만들고 있습니다. '학습' 탭에서 푸세요.")
            if val.get("report"):
                rep = val["report"]
                st.write(f"범위 {len(rep['asked'])}/{rep['tasks_total']} · 미출제 {len(rep['never_asked'])}개 "
                         "(자세한 내용은 '현황' 탭)")


def main() -> None:
    st.set_page_config(page_title="MissLens", page_icon="🔎", layout="wide")
    r = resources()
    st.session_state["_poll"] = False
    from misslens.paths import sandbox

    st.title("MissLens")
    st.caption(f"{r.pack.name} · 개념 실수와 해석 실수를 나눠 보는 학습 코치")
    if sandbox():
        st.warning("샌드박스 모드: 학습 기록·체크포인트는 data/sandbox/에 따로 저장되고, 자료 넣기·수집은 꺼져 있습니다.")
    sidebar_request(r)
    t_study, t_material, t_status, t_plan = st.tabs(["학습", "자료", "현황", "계획"])
    with t_plan:
        tab_plan(r)
    with t_material:
        tab_material(r)
    with t_status:
        tab_status(r)
    with t_study:
        tab_study(r)
    if st.session_state.pop("_poll", False):
        time.sleep(POLL_SECONDS)
        st.rerun()
