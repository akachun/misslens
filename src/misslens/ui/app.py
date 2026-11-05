"""Streamlit UI: Supervisor 그래프 위의 학습 · 자료 · 현황 · 계획 탭(SERVICE §5, ARCHITECTURE §10, T9).

에이전트 요청은 모두 Supervisor 그래프를 거치며, thread_id를 키로 하는 백그라운드 *job*으로 실행된다. 그래서
오래 걸리는 작업(수집, 생성 시작)이 페이지를 멈추지 않고, interrupt(계획 승인, 로그인 대기)는 checkpointer에서
다시 읽어 온다. 계획 thread_id와 진행 중인 set id는 URL(`?plan=<thread>&set=<id>`)에도 반영되므로, 새로고침해도
승인 화면이나 진행 중인 세트가 이어진다.

풀이와 리뷰 폼은 일반 위젯이다(interrupt 없음, CLAUDE.md 규칙 4). 보여 주는 문제는 ledger에서 가져온다
(status 'ready'만: validator와 근거 검증을 통과한 문제, 규칙 2).
"""

from __future__ import annotations

import html
import json
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
from misslens.agents.grader.vocab import KINDS, context_sentence, extract_vocab
from misslens.bedrock import PROJECT_ROOT
from misslens.ledger import miss_breakdown, set_score

LETTERS = "ABCD"
POLL_SECONDS = 1.5
UPLOAD_DIR = PROJECT_ROOT / "data" / "sources" / "uploads"
LABELS = {"reading": "해석 실수", "uncertain": "불확실", "concept": "개념 실수"}
USAGE_HELP = """| 탭 | 무엇을 하나 |
|---|---|
| **학습** | '이어 풀기'(제출 전 세트), '바로 풀기'(문제 수·영역), '지난 세트'(리뷰·진단 다시 보기). 풀이 → 제출 → 한국어 재질문 → 자기 판단 → 진단 → 코치에게 질문 → 이번 세트 단어장 |
| **단어장** | 세트에서 정답을 가른 영어 표현 누적. '외웠어요'로 숨기기 |
| **자료** | 파일·텍스트를 색인에 넣기, 로그인 세션으로 교안 수집 |
| **현황** | 범위, 정답률, 해석 실수 비율과 미끼 기준선, 놓친 문구, 진단 일치율 |
| **계획** | 오늘의 계획 제안 → 문제 수 고쳐 승인 → 세트 생성. '최근 계획' |

- 왼쪽 **MissLens에게 요청**: 자연어로 지시(예: \"도구 설계 영역 5문제\"). **Ctrl+Enter**(macOS ⌘+Enter)로 보내기, Enter는 줄바꿈.
- 코치 질문 칸도 Ctrl+Enter로 보냅니다.
- 푼 답은 문제를 넘길 때마다 저장되어, 창을 닫아도 '이어 풀기'로 돌아올 수 있습니다.
- 위쪽 ⏳ 줄에 진행 중인 작업(문제 생성, 단어장 등)이 한곳에 표시됩니다.
- 오른쪽 위 ⋮ 메뉴는 Streamlit 기본 메뉴입니다(화면 설정·인쇄 등). 학습 기능은 모두 위 탭에 있습니다."""
VERDICT_HELP = """처음 영어로 틀린 문제를 리뷰에서 **한국어로 다시** 풀어 판정합니다.

| 한국어로 다시 풀었을 때 | 판정 | 반영 |
|---|---|---|
| 맞힘 + 확신 | 해석 실수 | 해석 실수 비율, 처음 고른 오답이 어긴 조건을 '놓친 문구'로 기록 |
| 맞힘 + 추측 | 불확실 | 해석 비율에서 빼고 따로 셈(정답률에서는 오답) |
| 또 틀림 | 개념 실수 | 개념 실수로 셈 |

**미끼**는 처음에 맞힌 문제를 섞어 다시 물은 것입니다. 미끼를 한국어로 다시 맞힌 비율이 **미끼 기준선**이고, '다시 보면 맞히는 효과'를 따로 보여 줍니다. 해석 실수 비율은 이 기준선과 나란히 봅니다."""
SUBMIT_HINT = "Ctrl+Enter(macOS는 ⌘+Enter)로 보내기 · Enter는 줄바꿈"
# 긴 요청도 읽기 쉽게 사이드바를 넓힌다(사용자가 끌어서 더 넓힐 수도 있다). 펼쳐져 있을 때만 적용한다: Streamlit은
# 접힌 사이드바를 자기 너비만큼 밀어서 숨기므로, 거기에 고정 너비를 주면 빈 380px 띠가 남고
# 본문 영역이 넓어지지 않았다(시연 피드백).
SIDEBAR_CSS = "<style>section[data-testid='stSidebar'][data-collapsed='false']{min-width:380px}</style>"


# --- 리소스(프로세스당 하나) -------------------------------------------------------------

@dataclass
class Resources:
    pack: Any
    ledger: Any
    svc: Any                      # misslens.graph.Services
    graph: Any                    # checkpointer를 붙여 컴파일한 Supervisor 그래프
    explainer: Any = None
    index_stats: Callable[[], dict] = lambda: {}
    upload_dir: Path = UPLOAD_DIR
    jobs: dict = field(default_factory=dict)   # thread_id -> {"thread", "out", "error"}
    coach_checkpointer: Any = None             # 오답마다 코치 대화를 보관한다(None: 메모리)
    vocab_llm: Any = None                      # vocab 역할(D54). None이면 단어장 생성을 숨긴다

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
                     coach_checkpointer=sqlite_checkpointer(), vocab_llm=get_llm("vocab"))


RESOURCE_FACTORY: Callable[[], Resources] | None = None   # 테스트에서 바꿔 끼운다


@st.cache_resource
def resources() -> Resources:
    return (RESOURCE_FACTORY or default_resources)()


# --- supervisor job ---------------------------------------------------------------------------

def run_config(thread_id: str) -> dict:
    from misslens.trace import with_tracing

    return with_tracing({"configurable": {"thread_id": thread_id}, "recursion_limit": 2000})


def new_thread(kind: str) -> str:
    return f"ui-{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"


def start_job(r: Resources, thread_id: str, payload: Any) -> None:
    job = {"thread": None, "out": None, "error": None}

    def work() -> None:
        try:
            job["out"] = r.graph.invoke(payload, run_config(thread_id))
        except Exception as exc:  # noqa: BLE001 - 학습자에게 보여 주며, 삼키지 않는다
            job["error"] = f"{type(exc).__name__}: {exc}"

    job["thread"] = threading.Thread(target=work, name=f"job-{thread_id}", daemon=True)
    r.jobs[thread_id] = job
    job["thread"].start()


def job_state(r: Resources, thread_id: str) -> tuple[str, Any]:
    """-> ("running", None) | ("error", msg) | ("interrupt", payload) | ("done", values) | ("none", None).
    job이 메모리에 없으면(재시작 후 페이지 새로고침) checkpointer에서 읽는다."""
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
    """패널이 백그라운드 작업을 기다리는 중임을 표시한다. 그러면 페이지는 작은 상태 fragment 하나로 폴링하고,
    실제로 무언가 바뀌었을 때만 rerun한다(D55). 한 탭의 긴 job이 다른 탭을 가리지 않는다."""
    st.session_state["_poll"] = True


# "fragment": 상태 fragment에서 폴링하고, 바뀌었을 때만 전체 rerun(브라우저). "rerun": 매 렌더링 뒤
# sleep + 전체 rerun(AppTest는 run_every fragment를 돌릴 수 없어서 테스트에서 쓴다).
POLL_MODE = "fragment"
JOB_LABELS = {"ui-ask": "요청 처리", "ui-plan": "계획 세우기", "ui-quiz": "세트 준비", "ui-resume": "문제 생성 재개",
              "ui-collect": "교안 수집", "ui-ingest": "자료 색인", "vocab": "단어장 만들기"}


def background_signature(r: Resources) -> tuple:
    """기다리는 패널이 의존하는 값: 어떤 job이 살아 있거나 실패했는지, 살아 있는 stream마다 어디까지 왔는지."""
    jobs = tuple(sorted((k, bool(j.get("thread") and j["thread"].is_alive()), bool(j.get("error")))
                        for k, j in r.jobs.items()))
    streams = tuple(sorted((sid, s.running, r.ledger.set_progress(sid)["ready"])
                           for sid, s in r.svc.streams.items() if s.running))
    return jobs, streams


def activity_lines(r: Resources) -> list[str]:
    out = [f"세트 #{sid} 문제 생성 {p['ready']}/{p['size']}"
           for sid, s in sorted(r.svc.streams.items()) if s.running for p in [r.ledger.set_progress(sid)]]
    for k, j in r.jobs.items():
        if j.get("thread") and j["thread"].is_alive():
            label = next((v for pre, v in JOB_LABELS.items() if k.startswith(pre)), "작업")
            if label not in out:
                out.append(label)
    return out


def status_ticker(r: Resources) -> None:
    """유일한 로딩 표시: 페이지를 다시 그리지 않고 POLL_SECONDS마다 스스로 새로 고친다."""
    @st.fragment(run_every=POLL_SECONDS)
    def _tick() -> None:
        lines = activity_lines(r)
        st.caption("⏳ 진행 중: " + " · ".join(lines) if lines else " ")
        if background_signature(r) != st.session_state.get("_sig"):
            st.rerun(scope="app")

    _tick()


# --- 풀이 세션 ------------------------------------------------------------------------------

def start_quiz(r: Resources, set_id: int) -> None:
    """세트를 현재 상태 그대로 연다: 임시 저장한 답은 되살리고, 제출한 세트는 리뷰/진단으로 보낸다(D52)."""
    s = r.ledger.get_set(set_id)
    drafts = r.ledger.draft_answers(set_id)
    st.session_state.quiz = {
        "set_id": set_id, "n": s["size"], "shown": {}, "notes": [], "items": None,
        "answers": {qid: {"question_id": qid, "chosen_index": d["chosen_index"], "elapsed_ms": d["elapsed_ms"]}
                    for qid, d in drafts.items()},
        "phase": {"submitted": "review", "reviewed": "diagnosis"}.get(s["status"], "solving")}
    st.query_params["set"] = str(set_id)


def adopt_result(values: dict, thread_id: str) -> None:
    """끝난 Supervisor 실행을 한 번만 처리한다: 새 세트면 풀이를 시작한다."""
    done = st.session_state.setdefault("adopted", set())
    if thread_id in done:
        return
    done.add(thread_id)
    sid = values.get("set_id")
    if sid is not None and values.get("score") is None:
        start_quiz(resources(), int(sid))


def ready_questions(r: Resources, set_id: int) -> list[dict]:
    return [q for q in r.ledger.set_questions(set_id) if q["status"] == "ready"]


def tab_study(r: Resources) -> None:
    quiz = st.session_state.get("quiz")
    if quiz is None and st.query_params.get("set"):
        start_quiz(r, int(st.query_params["set"]))
        quiz = st.session_state.quiz
    if quiz is None:
        study_start(r)
        return
    {"solving": study_solve, "review": study_review, "self_check": study_self_check,
     "diagnosis": study_diagnosis}[quiz["phase"]](r, quiz)


def request_resume(r: Resources, set_id: int) -> None:
    """stream이 사라진(서버 재시작) 세트의 생성을 다시 시작하고 세트를 연다."""
    prog = r.ledger.set_progress(set_id)
    live = r.svc.streams.get(set_id)
    if (prog["status"] in ("generating", "in_progress") and prog["ready"] < prog["size"]
            and (live is None or not live.running)):
        start_job(r, new_thread("resume"), {"intent": "resume", "args": {"set_id": set_id}})
    start_quiz(r, set_id)


def open_sets_panel(r: Resources) -> None:
    sets = r.ledger.open_sets(r.pack.exam_id)
    if not sets:
        return
    st.subheader("이어 풀기")
    for p in sets:
        live = r.svc.streams.get(p["id"])
        gen = "생성 중" if live is not None and live.running else (
            "생성 멈춤" if p["ready"] < p["size"] else "생성 완료")
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**세트 #{p['id']}** · 준비 {p['ready']}/{p['size']} ({gen}) · 푼 문제 {p['drafts']} · "
                    f"{p['created_at'][:16].replace('T', ' ')} UTC")
        if c2.button("이어 풀기", key=f"resume_{p['id']}"):
            request_resume(r, p["id"])
            st.rerun()


ORIGIN_KO = {"plan": "계획", "quick": "바로 풀기", "ask": "자연어 요청", "cli": "CLI", "unknown": "이전 기록"}


def past_sets_panel(r: Resources) -> None:
    """제출한 모든 세트를, 만든 방식(계획, 바로 풀기, 자연어, CLI)과 상관없이 최신순으로 보여 준다(D53)."""
    sets = r.ledger.list_sets(r.pack.exam_id)
    if not sets:
        return
    st.subheader("지난 세트")
    for p in sets:
        score = set_score(r.ledger, p["id"])
        mb = miss_breakdown(r.ledger, r.pack.exam_id, set_id=p["id"])
        doms = ", ".join(f"{d} {n}" for d, n in (p["counts"] or {}).items() if n)
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**세트 #{p['id']}** · {ORIGIN_KO[p['origin']]}{' (' + doms + ')' if doms else ''} · "
                    f"{score.correct}/{score.total} 정답 · 개념 {mb.concept} · 해석 {mb.reading} · "
                    f"{STATUS_KO.get(p['status'], p['status'])} · {p['created_at'][:16].replace('T', ' ')} UTC")
        if c2.button("리뷰하기" if p["status"] == "submitted" else "진단 보기", key=f"past_{p['id']}"):
            start_quiz(r, p["id"])
            st.rerun()


def study_start(r: Resources) -> None:
    open_sets_panel(r)
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
    past_sets_panel(r)


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
    head, back = st.columns([5, 1])
    head.subheader(f"세트 #{sid} · {len(ready)}/{quiz['n']}문제 준비 · 푼 문제 {len(quiz['answers'])}")
    if back.button("목록으로", key=f"to_list_{sid}", help="푼 답은 저장돼 있어 '이어 풀기'로 돌아올 수 있습니다"):
        reset_quiz()
    for note in quiz["notes"]:
        st.caption(note)
    # 다음 문제는 답이 없는 첫 문제이므로, 이어 푸는 세트는 멈춘 곳부터 계속된다.
    quiz["idx"] = next((i for i, q in enumerate(ready) if q["id"] not in quiz["answers"]), len(ready))
    if quiz["idx"] < len(ready):
        if not quiz.get("started"):
            # 첫 문제가 화면에 나오면 generating -> in_progress로 바꾼다. CLI와 같다(§4.1).
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
            ans = {"question_id": q["id"], "chosen_index": choice,
                   "elapsed_ms": int((time.monotonic() - quiz["shown"][q["id"]]) * 1000)}
            r.ledger.save_draft_answer(sid, q["id"], choice, ans["elapsed_ms"])   # 탭을 닫거나 재시작해도 남는다
            quiz["answers"][q["id"]] = ans
            st.rerun()
        return
    if generating:
        st.info("다음 문제를 생성하는 중...")
        wait_rerun()
        return
    if len(ready) < quiz["n"]:
        st.warning(f"문제 생성이 멈췄습니다({len(ready)}/{quiz['n']}). 서버가 다시 시작됐거나 생성이 연속 실패했을 수 "
                   "있습니다. 남은 문제를 다시 만들거나, 지금까지 준비된 문제로 제출할 수 있습니다.")
        if st.button("남은 문제 생성", key=f"regen_{sid}"):
            start_job(r, new_thread("resume"), {"intent": "resume", "args": {"set_id": sid}})
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
    """오답마다 학습자가 직접 붙이는 라벨: 정답은 보여 주고 판정과 해설은 아직 보여 주지 않는다(D43, eval 데이터)."""
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
    with st.expander("판정은 어떻게 정해지나요?"):
        st.markdown(VERDICT_HELP)
    invalid = {a["id"] for a in r.ledger.set_attempts(sid) if a["invalid"]}
    for row in rows:
        q = row.question
        if row.is_correct:
            decoy = ""
            if row.is_decoy and row.reask_choice is not None:
                decoy = " · 한국어로 다시 " + ("맞힘" if row.reask_choice == q["answer_index"] else "틀림")
            with st.expander(f"✅ 문제 {row.seq + 1} 정답" + (" (미끼)" if row.is_decoy else "") + decoy
                             + f" · 정답 {LETTERS[q['answer_index']]}", expanded=False):
                if row.reask_text:
                    st.caption(row.reask_text + " — 미끼 기준선에 들어갑니다")
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
            if row.reask_text:
                st.caption(row.reask_text + f" → {LABELS.get(row.miss_type, '-')}")
            if row.missed_phrase:
                st.markdown(f"**놓친 조건:** <mark>{html.escape(row.missed_phrase)}</mark>", unsafe_allow_html=True)
            st.markdown("**해설**")
            st.write(q["explanation_native"])
            coach_panel(r, row)
            if row.attempt_id not in invalid and st.button("문제 이상(통계에서 빼기)", key=f"inv_{row.attempt_id}"):
                r.ledger.mark_invalid(row.attempt_id)
                st.rerun()
    set_vocab_panel(r, sid)
    if st.button("세트 목록으로", key="new_set", type="primary"):
        reset_quiz()


KIND_KO = {"condition": "조건 표현", "technical": "기술 용어", "general": "일반 어휘"}
VOCAB_NOTE = "AI가 지문에서 고른 표현과 뜻입니다(AI 생성). 헷갈리면 해당 문제에서 코치에게 물어보세요."


def _toggle_known(term_key: str, widget_key: str) -> None:
    r = resources()
    r.ledger.mark_known(r.pack.exam_id, term_key, bool(st.session_state.get(widget_key)))


def vocab_row(item: dict, key: str, stem: str | None = None, extra: str = "") -> None:
    c1, c2 = st.columns([6, 1])
    badge = " · 🔸 놓친 문제" if item.get("from_miss") else ""
    c1.markdown(f"**{html.escape(item['term'])}** · {KIND_KO.get(item['kind'], item['kind'])} · "
                f"{html.escape(item['meaning'])}{badge}{extra}", unsafe_allow_html=True)
    if item.get("role"):
        c1.caption(item["role"])
    sentence = context_sentence(stem, item["term"]) if stem else None
    if sentence:
        c1.markdown("> " + highlight(html.escape(sentence), html.escape(item["term"]), "<mark>", "</mark>"),
                    unsafe_allow_html=True)
    c2.checkbox("외웠어요", value=bool(item.get("known")), key=key, on_change=_toggle_known,
                args=(item["term_key"], key))


def set_vocab_panel(r: Resources, sid: int) -> None:
    """진단 아래의 '이번 세트 단어장': 백그라운드에서 한 번 만들고, 그다음부터는 ledger에서 읽는다(D54)."""
    st.subheader("이번 세트 단어장")
    items = r.ledger.set_vocab(sid)
    job_key = f"vocab-{sid}"
    job = r.jobs.get(job_key)
    if not items:
        if r.vocab_llm is None:
            st.caption("단어장 모델이 설정되지 않았습니다.")
            return
        if job is None:
            job = {"thread": None, "out": None, "error": None}

            def work() -> None:
                try:
                    extract_vocab(r.vocab_llm, r.pack, r.ledger, sid)
                except Exception as exc:  # noqa: BLE001 - 다시 시도 버튼과 함께 보여 준다
                    job["error"] = f"{type(exc).__name__}: {exc}"[:300]

            job["thread"] = threading.Thread(target=work, name=job_key, daemon=True)
            r.jobs[job_key] = job
            job["thread"].start()
        if job["thread"].is_alive():
            st.info("이번 세트에서 정리할 영어 표현을 고르는 중...")
            wait_rerun()
        elif job["error"]:
            st.error(f"단어장을 만들지 못했습니다: {job['error']}")
            if st.button("단어장 다시 만들기", key=f"vocab_retry_{sid}"):
                r.jobs.pop(job_key, None)
                st.rerun()
        else:
            st.caption("정리할 표현을 찾지 못했습니다.")
        return
    st.caption(VOCAB_NOTE)
    for kind in KINDS:
        group = [it for it in items if it["kind"] == kind]
        if not group:
            continue
        st.markdown(f"**{KIND_KO[kind]}** ({len(group)})")
        for it in group:
            stem = json.loads(it["payload_json"])["stem_en"]
            vocab_row(it, f"vk_{sid}_{it['term_key']}", stem, f" · 문제 {it['seq'] + 1}")


def tab_vocab(r: Resources) -> None:
    """세트를 넘나들며 쌓이는 누적 단어장(D54)."""
    book = r.ledger.vocab_book(r.pack.exam_id)
    if not book:
        st.info("아직 단어장이 없습니다. 세트를 풀고 진단 화면까지 가면 그 세트의 단어장이 만들어집니다.")
        return
    c1, c2, c3 = st.columns([2, 2, 3])
    kinds = c1.multiselect("종류", list(KINDS), default=list(KINDS), format_func=KIND_KO.get, key="vb_kinds")
    hide = c2.checkbox("외운 단어 숨기기", value=True, key="vb_hide")
    q = c3.text_input("찾기", key="vb_q", placeholder="영어 표현이나 뜻")
    rows = [b for b in book if b["kind"] in kinds and not (hide and b["known"])
            and (not q or q.lower() in b["term"].lower() or q in b["meaning"])]
    known = sum(1 for b in book if b["known"])
    st.caption(f"전체 {len(book)}개 · 외움 {known}개 · 표시 {len(rows)}개 · 놓친 문제에서 나온 표현 먼저, 그다음 자주 나온 순. "
               + VOCAB_NOTE)
    for b in rows:
        vocab_row(b, f"vb_{b['term_key']}", b["uses"][0]["stem_en"] if b["uses"] else None,
                  f" · {b['seen']}회(세트 {b['sets']}개)")


def source_links(sources: list[str]) -> str:
    """Markdown: URL은 학습자가 열 수 있는 링크가 된다(Skilljar 강의는 로그인된 브라우저에서 열린다)."""
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
    """저장된 해설 아래에 코치 해설을, 이어서 같은 오답에 대한 후속 대화를 보여 준다(D47)."""
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
            if not coach_history(r.coach_checkpointer, tid):     # 코치 실패: 일반 해설만, thread 없음
                st.session_state[f"coach_fallback_{row.attempt_id}"] = res.text
            st.rerun()
    elif turns[0].question is None:                              # 코치 해설로 시작한 대화
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
        question = st.text_area("코치에게 질문", key=f"q_{row.attempt_id}", height=90,
                                placeholder="예: tool_choice any와 auto는 언제 다르게 쓰나요?")
        st.caption(SUBMIT_HINT)
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


# --- 계획 탭(interrupt①) ---------------------------------------------------------------------

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
        plan_history(r)
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


STATUS_KO = {"generating": "생성 중", "in_progress": "푸는 중", "submitted": "리뷰 전", "reviewed": "진단 완료"}


def plan_history(r: Resources) -> None:
    """승인한 계획을 최신순으로, 각 계획이 만든 세트와 함께 보여 준다(D52)."""
    plans = r.ledger.list_plans(r.pack.exam_id)
    if not plans:
        return
    st.subheader("최근 계획")
    names = {d.id: d.name_native or d.name for d in r.pack.domains}
    for p in plans:
        s = p["set"] or {}
        state = STATUS_KO.get(s.get("status"), "-")
        title = (f"{p['created_at'][:16].replace('T', ' ')} UTC · {p['total']}문제 · 세트 #{p['set_id']} {state}"
                 + (f" (준비 {s['ready']}/{s['size']}, 푼 문제 {s['drafts']})" if s.get("status") in
                    ("generating", "in_progress") else ""))
        with st.expander(title):
            st.dataframe([{"영역": f"{d} {names.get(d, '')}", "문제 수": n}
                          for d, n in (p["counts"] or {}).items() if n], hide_index=True)
            if p["rationale"]:
                st.write(p["rationale"])
            label = "이어 풀기" if s.get("status") in ("generating", "in_progress") else "결과 보기"
            if p["set_id"] and st.button(label, key=f"plan_go_{p['id']}"):
                request_resume(r, p["set_id"])
                st.success(f"'학습' 탭에서 세트 #{p['set_id']}를 이어서 보세요.")


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


# --- 자료 탭 ------------------------------------------------------------------------------

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


# --- 현황 탭 ---------------------------------------------------------------------------------

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


# --- 사이드바: 자연어 요청(F10) ----------------------------------------------------

def sidebar_request(r: Resources) -> None:
    with st.sidebar:
        st.markdown("### MissLens에게 요청")
        # 폼으로 감싼다: Ctrl+Enter(macOS는 ⌘+Enter)로 text_area를 보내고, 그냥 Enter는 줄바꿈으로 남는다.
        with st.form("nl_form", clear_on_submit=True, border=False):
            text = st.text_area("요청", key="nl_text", height=130, label_visibility="collapsed",
                                placeholder="예: 오늘 공부 계획 세워줘 / 도구 설계 영역 5문제 / 현황 보여줘")
            sent = st.form_submit_button("보내기", type="primary", use_container_width=True)
        st.caption(SUBMIT_HINT)
        if sent and text.strip():
            tid = new_thread("ask")
            st.session_state.nl_thread = tid
            st.session_state.nl_last = text.strip()
            start_job(r, tid, {"text": text.strip()})
        tid = st.session_state.get("nl_thread")
        if not tid:
            return
        if st.session_state.get("nl_last"):
            st.markdown(f"**보낸 요청**  \n{html.escape(st.session_state['nl_last'])}")
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
    st.set_page_config(page_title="MissLens", page_icon="🔎", layout="wide", menu_items={
        "About": "**MissLens** — 자격증 시험 대비 학습 코치. 틀린 문제를 한국어로 다시 풀어 **개념 실수와 해석 실수**를 "
                 "나누고, 놓친 조건 문구와 영어 표현을 정리해 약점 위주로 다시 출제합니다. 사용법은 화면 위 '사용법'을 펼치세요."})
    st.markdown(SIDEBAR_CSS, unsafe_allow_html=True)
    r = resources()
    st.session_state["_poll"] = False
    from misslens.paths import sandbox

    st.title("MissLens")
    st.caption(f"{r.pack.name} · 개념 실수와 해석 실수를 나눠 보는 학습 코치")
    with st.expander("사용법"):
        st.markdown(USAGE_HELP)
    if sandbox():
        st.warning("샌드박스 모드: 학습 기록·체크포인트는 data/sandbox/에 따로 저장되고, 자료 넣기·수집은 꺼져 있습니다.")
    status_box = st.container()
    sidebar_request(r)
    t_study, t_vocab, t_material, t_status, t_plan = st.tabs(["학습", "단어장", "자료", "현황", "계획"])
    with t_plan:
        tab_plan(r)
    with t_vocab:
        tab_vocab(r)
    with t_material:
        tab_material(r)
    with t_status:
        tab_status(r)
    with t_study:
        tab_study(r)
    if st.session_state.pop("_poll", False):
        if POLL_MODE == "fragment":
            st.session_state["_sig"] = background_signature(r)     # 이번 렌더링이 이미 보여 주는 상태
            with status_box:
                status_ticker(r)
        else:
            time.sleep(POLL_SECONDS)
            st.rerun()
