"""MissLens CLI(Streamlit 없이 도는 핵심 루프).

    python -m misslens.cli quiz [-n 5] [--exam ccar-f]   # 세트 -> 풀이 -> 제출 -> 다시 풀기 -> 진단
    python -m misslens.cli status [--exam ccar-f]        # ledger의 간단한 수치
    python -m misslens.cli ingest PATH... [--paste]      # 학습 자료(파일이나 폴더)를 색인에 넣는다
    python -m misslens.cli search "query" [--task 2.1]   # 검색이 무엇을 돌려주는지 확인한다
    python -m misslens.cli plan [-n 10]                  # Planner 초안 -> 승인(interrupt①) -> 풀이
    python -m misslens.cli ask "도구 설계 5문제"          # 자연어 -> Supervisor 라우터 -> 에이전트
    python -m misslens.cli retrieval-eval [--save-baseline]  # 검색 점수 + 색인 건강 vs 기준선(D50)
    python -m misslens.cli eval [--export]               # 진단 vs 학습자가 직접 붙인 라벨(T10 eval)
    python -m misslens.cli practice import [DIR]         # 학습자가 준 연습 문제 세트를 문체 은행으로 변환한다
    python -m misslens.cli collect login skilljar        # 보이는 브라우저에서 직접 로그인한다(프로필만 저장)
    python -m misslens.cli collect run [--site skilljar] # sources.yaml 페이지를 수집한 뒤 색인에 넣는다

interrupt는 `collect run`(로그인 대기, interrupt②)과 이후의 Planner에만 있다(CLAUDE.md 규칙 4).

풀이와 리뷰는 일반 입력 프롬프트이며, 절대 interrupt가 아니다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from misslens.agents.generator import Generator, QuestionStream
from misslens.agents.grader import build_reask_form, diagnosis_rows, highlight, submit_review
from misslens.agents.grader.coach import coach_explain
from misslens.exam_pack import ExamPack, load_exam_pack
from misslens.ledger import Answer, Ledger, miss_breakdown, set_score

LETTERS = "ABCD"
Ask = Callable[[str], str]


class CLI:
    def __init__(self, pack: ExamPack, ledger: Ledger, *, console: Console | None = None, ask: Ask | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.pack = pack
        self.ledger = ledger
        self.console = console or Console()
        self._ask = ask or (lambda prompt: self.console.input(prompt))
        self.clock = clock
        self.index = None   # 코치 검색 도구용 ChunkIndex(D44). None이면 코치는 아무것도 검색하지 않는다

    def ask_choice(self, prompt: str, *, allow_blank: bool = True) -> int | None:
        while True:
            raw = self._ask(prompt).strip().upper()
            if raw == "" and allow_blank:
                return None
            if len(raw) == 1 and raw in LETTERS:
                return LETTERS.index(raw)
            self.console.print("A, B, C, D 중 하나를 입력하세요." + (" (건너뛰려면 Enter)" if allow_blank else ""))

    def ask_yes_no(self, prompt: str) -> bool:
        while True:
            raw = self._ask(prompt).strip().lower()
            if raw in ("y", "yes", "ㅛ", "확신"):
                return True
            if raw in ("n", "no", "ㅜ", "추측"):
                return False
            self.console.print("y(확신) 또는 n(추측)으로 답하세요.")

    # --- 풀이 ------------------------------------------------------------------------------

    def quiz(self, n: int, generator: Generator, *, explainer: Any = None) -> int | None:
        set_id = self.ledger.create_set(self.pack.exam_id, n)
        self.ledger.record_plan(self.pack.exam_id, set_id=set_id, kind="quick", total=n, counts=None,
                                thread_id="cli-quiz")       # 세트가 출처와 함께 기록 목록에 나오게 한다
        stream = QuestionStream(generator, set_id, n).start()
        return self.run_stream(set_id, n, stream, explainer=explainer)

    def run_stream(self, set_id: int, n: int, stream: QuestionStream, *, explainer: Any = None) -> int | None:
        """세트가 생성되는 동안 풀고, 제출한 뒤 리뷰한다(interrupt 없음, 규칙 4)."""
        c = self.console
        generator = stream.generator
        c.print(Panel(f"{self.pack.name} · {n}문제 세트 #{set_id}\n"
                      "실전처럼 영어 지문으로 풉니다. 해설은 세트를 제출한 뒤 리뷰에서 보여 줍니다.", title="MissLens"))
        answers: list[Answer] = []
        started = False
        try:
            while True:
                with c.status("다음 문제를 생성하는 중..."):
                    ev = stream.next()
                if ev.kind == "ready":
                    if not started:
                        self.ledger.start_set(set_id)
                        started = True
                    # seq로 번호를 매겨 풀이, 진단, '문제 이상'이 같은 번호를 쓰게 한다.
                    answers.append(self._solve(ev.seq + 1, ev.question_id, ev.question))
                elif ev.kind == "failed":
                    c.print(f"[red]문제 {ev.seq + 1} 생성 실패[/]: {escape(ev.message)}")
                else:
                    c.print(("[yellow]" if ev.kind == "stopped" else "[green]") + escape(ev.message) + "[/]")
                    break
        except (KeyboardInterrupt, EOFError):
            generator.cancel()          # main()이 ledger를 닫기 전에 ledger 쓰기를 멈춘다
            stream.join(timeout=30)
            raise
        stream.join(timeout=5)
        if not answers:
            c.print("풀 문제가 없습니다. `status`에서 생성 실패 항목을 확인하세요.")
            return None
        self.ledger.submit_set(set_id, answers)
        score = set_score(self.ledger, set_id)
        c.print(Panel(f"제출 완료: {score.total}문제 중 {score.correct}문제 정답", title=f"세트 #{set_id}"))
        self.review(set_id, explainer=explainer)
        return set_id

    def _solve(self, number: int, question_id: int, q) -> Answer:
        c = self.console
        body = escape(q.stem_en) + "\n\n" + "\n".join(f"{LETTERS[i]}. {escape(o.text_en)}" for i, o in enumerate(q.options))
        c.print(Panel(body, title=f"문제 {number} · {q.task_id}"))
        t0 = self.clock()
        choice = self.ask_choice("답 (A-D, 건너뛰기 Enter): ")
        return Answer(question_id, choice, int((self.clock() - t0) * 1000))

    # --- 리뷰 ----------------------------------------------------------------------------

    def review(self, set_id: int, *, explainer: Any = None) -> None:
        c = self.console
        items, responses = [], []
        if self.pack.needs_reask:
            items = build_reask_form(self.ledger.set_questions(set_id), self.ledger.set_attempts(set_id))
            if items:
                c.print(Panel("몇 문제를 한국어로 다시 풉니다. 맞힌 문제도 섞여 있습니다.\n"
                              "답과 함께 확신(y) / 추측(n)을 알려 주세요.", title="리뷰: 다시 풀기"))
            for i, item in enumerate(items, 1):
                body = escape(item.stem) + "\n\n" + "\n".join(f"{LETTERS[j]}. {escape(o)}" for j, o in enumerate(item.options))
                c.print(Panel(body, title=f"다시 풀기 {i}/{len(items)}"))
                choice = self.ask_choice("답 (A-D): ", allow_blank=False)
                responses.append((choice, self.ask_yes_no("확신하나요? (y=확신 / n=추측): ")))
        submit_review(self.ledger, self.pack, set_id, items, responses)
        self.self_check(set_id)
        self.show_diagnosis(set_id, explainer=explainer)

    SELF_CHECK_PROMPT = "정답을 보니: c=몰랐던 내용(개념) · r=알았는데 조건을 놓침(해석) · u=모르겠음 · Enter=건너뛰기 > "

    def self_check(self, set_id: int) -> int:
        """오답마다 학습자가 직접 붙이는 라벨. 정답을 본 뒤, 판정과 해설을 보기 전에 받는다(D43).
        공부하면서 진단 eval 데이터를 쌓는다. 저장한 라벨 수를 돌려준다."""
        c, saved = self.console, 0
        misses = [r for r in diagnosis_rows(self.ledger, set_id) if not r.is_correct and r.miss_type]
        if misses:
            c.print(Panel("틀린 문제의 정답을 먼저 봅니다. 해설을 보기 전에, 왜 틀렸다고 생각하는지 골라 주세요.\n"
                          "(진단이 맞는지 확인하는 데만 쓰고, 판정은 바꾸지 않습니다)", title="자기 판단"))
        for r in misses:
            q = r.question
            chosen = "-" if r.chosen_index is None else f"{LETTERS[r.chosen_index]}. {q['options'][r.chosen_index]['text_en']}"
            right = f"{LETTERS[q['answer_index']]}. {q['options'][q['answer_index']]['text_en']}"
            c.print(Panel(f"{escape(q['stem_en'])}\n\n고른 답: {escape(chosen)}\n정답: {escape(right)}",
                          title=f"문제 {r.seq + 1}"))
            while True:
                a = self._ask(self.SELF_CHECK_PROMPT).strip().lower()
                label = {"c": "concept", "r": "reading", "u": "unsure"}.get(a)
                if a == "" or label:
                    break
            if label:
                self.ledger.record_miss_feedback(r.attempt_id, label)
                saved += 1
        return saved

    def show_diagnosis(self, set_id: int, *, explainer: Any = None) -> None:
        c = self.console
        labels = {"reading": "[yellow]해석 실수[/]", "uncertain": "[magenta]불확실[/]", "concept": "[red]개념 실수[/]"}
        rows = diagnosis_rows(self.ledger, set_id)
        for r in rows:
            q = r.question
            if r.is_correct:
                c.print(f"[green]문제 {r.seq + 1} 정답[/]" + (" (미끼)" if r.is_decoy else ""))
                continue
            chosen = "-" if r.chosen_index is None else LETTERS[r.chosen_index]
            head = f"문제 {r.seq + 1} · {labels.get(r.miss_type, r.miss_type)} · 고른 답 {chosen} → 정답 {LETTERS[q['answer_index']]}"
            if r.reask_text:
                head += f" · {r.reask_text}"
            stem = highlight(escape(q["stem_en"]), escape(r.missed_phrase) if r.missed_phrase else None)
            extra = f"\n\n놓친 조건: [bold yellow]{escape(r.missed_phrase)}[/]" if r.missed_phrase else ""
            if explainer is not None:
                with c.status("코치가 자료를 찾아 해설을 쓰는 중..."):
                    res = coach_explain(explainer, self.pack, r, ledger=self.ledger, index=self.index)
                text = res.text
                if res.sources:
                    extra += "\n\n[dim]근거: " + escape(", ".join(res.sources)) + "[/]"
            else:
                text = q["explanation_native"]
            c.print(Panel(f"{stem}{extra}\n\n{escape(text)}", title=head))
        mb = miss_breakdown(self.ledger, self.pack.exam_id, set_id=set_id)
        base = "-" if mb.baseline is None else f"{mb.baseline:.0%} ({mb.decoys_correct}/{mb.decoys})"
        c.print(f"개념 {mb.concept} · 해석 {mb.reading} · 불확실 {mb.uncertain} · 미끼 기준선 {base}")
        raw = self._ask("문제 이상으로 표시할 문제 번호(쉼표로 구분, 없으면 Enter): ").strip()
        by_seq = {r.seq + 1: r for r in rows}
        for part in filter(None, (p.strip() for p in raw.split(","))):
            if part.isdigit() and int(part) in by_seq:
                self.ledger.mark_invalid(by_seq[int(part)].attempt_id)
                c.print(f"문제 {part}을(를) 통계에서 뺐습니다.")
            else:
                c.print(f"[red]알 수 없는 번호: {escape(part)}[/]")

    # --- 현황 ----------------------------------------------------------------------------

    def status(self) -> None:
        from misslens.agents.reporter import build_report

        self.render_report(build_report(self.pack, self.ledger).as_dict())

    def render_report(self, r: dict) -> None:
        """Reporter 수치를 그리는 하나뿐인 렌더러(CLI status와 Supervisor의 status 경로가 함께 쓴다)."""
        c = self.console
        ratio = "-" if r["reading_ratio"] is None else f"{r['reading_ratio']:.0%}"
        base = "-" if r["baseline"] is None else f"{r['baseline']:.0%}"
        c.print(f"범위 {len(r['asked'])}/{r['tasks_total']} ({r['coverage_ratio']:.0%}) · 해석 실수 비율 {ratio} "
                f"(미끼 기준선 {base}) · 개념 {r['concept']} · 해석 {r['reading']} · 불확실 {r['uncertain']}")
        if r["avg_time_ms"] is not None:
            c.print(f"평균 풀이 시간 {r['avg_time_ms'] / 1000:.0f}초")
        tbl = Table("영역", "비중", "출제 항목", "풀이", "정답률(보정)")
        for d in r["domains"]:
            tbl.add_row(f"{d['domain']} {escape(d['name'])}", f"{d['weight']:.0%}", f"{d['asked']}/{d['tasks']}",
                        str(d["attempts"]), f"{d['accuracy']:.0%}")
        c.print(tbl)
        if r["top_phrases"]:
            tbl = Table("놓친 문구", "횟수")
            for p, n in r["top_phrases"]:
                tbl.add_row(escape(p), str(n))
            c.print(tbl)
        ag = r.get("agreement") or {}
        if ag.get("labeled"):
            rate = "-" if ag["rate"] is None else f"{ag['rate']:.0%}"
            c.print(f"진단 일치율(내 판단과) {rate} ({ag['agree']}/{ag['compared']}, 판단 남긴 오답 {ag['labeled']}개)")
        if r["never_asked"]:
            c.print("미출제: " + ", ".join(r["never_asked"]))
        for f in r["failures"]:
            c.print(f"[red]생성 실패[/] {f['task_id']} ({f['tries']}회): {escape(f['reason'][:160])}")

    # --- 계획 승인(interrupt①) ---------------------------------------------------------

    def ask_plan(self, payload: dict) -> dict:
        """초안 계획을 보여 주고, planner의 interrupt에 넘길 resume 값을 돌려준다."""
        c, plan = self.console, payload["plan"]
        names = {d["domain"]: d for d in payload.get("table", [])}
        tbl = Table("영역", "비중", "출제 항목", "정답률(보정)", "오늘 문제 수", title=f"오늘의 계획 · {plan['total']}문제")
        for dom, n in plan["counts"].items():
            d = names.get(dom, {})
            tbl.add_row(f"{dom} {escape(d.get('name', ''))}", f"{d.get('weight', 0):.0%}",
                        f"{d.get('asked', 0)}/{d.get('tasks', 0)}", f"{d.get('accuracy', 0.5):.0%}", str(n))
        c.print(tbl)
        c.print(escape(plan.get("rationale", "")))
        if payload.get("error"):
            c.print(f"[red]{escape(payload['error'])}[/]")
        while True:
            a = self._ask("Enter=승인, e=문제 수 고치기, q=취소 > ").strip().lower()
            if a == "":
                return {"action": "approve"}
            if a in ("q", "quit"):
                return {"action": "cancel"}
            if a == "e":
                doms = list(plan["counts"])
                raw = self._ask(f"{' '.join(doms)} 순서로 문제 수를 공백으로 구분해 입력 > ").split()
                if len(raw) == len(doms) and all(x.lstrip("-").isdigit() for x in raw):
                    return {"action": "approve", "counts": dict(zip(doms, map(int, raw)))}
                c.print(f"{len(doms)}개의 정수를 입력하세요.")


# --- 자료 ------------------------------------------------------------------------------

def expand_paths(paths: list[str]) -> list[Path]:
    """파일은 주어진 그대로, 폴더는 안의 지원 파일을 돌려준다(정렬, 하위 폴더 포함)."""
    from misslens.agents.ingest.loaders import SUPPORTED

    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED))
        else:
            out.append(p)
    return out


def run_ingest(ingestor: Any, docs: list[Any], console: Console) -> int:
    """문서를 색인에 넣고 문서마다 한 줄과 세부 항목별 자료 수를 출력한다. 추가한 chunk 수를 돌려준다."""
    added = 0
    for doc in docs:
        r = ingestor.ingest(doc)
        label = {"added": f"[green]추가[/] {r.chunks}개 청크", "duplicate": "[yellow]이미 있음[/]",
                 "empty": "[red]본문 없음[/]"}[r.status]
        console.print(f"{label} · {escape(r.source)}")
        added += r.chunks
    counts = ingestor.index.tag_counts()
    pack = ingestor.pack
    missing = [t for t in pack.task_ids if t not in counts]
    console.print(f"색인 {len(ingestor.index)}개 청크 · 자료가 있는 세부 항목 {len(pack.task_ids) - len(missing)}"
                  f"/{len(pack.task_ids)} · 미분류 {counts.get('unassigned', 0)}")
    if missing:
        console.print("자료 없는 세부 항목: " + ", ".join(missing))
    return added


def open_index(exam_id: str, *, must_exist: bool = False):
    """시험의 색인. 아직 넣은 자료가 없으면 None이다(bge-m3를 괜히 불러오지 않는다)."""
    from misslens.retrieval.index import INDEX_ROOT, ChunkIndex

    if not (INDEX_ROOT / exam_id / "meta.json").exists() and not must_exist:
        return None
    from misslens.retrieval.embed import get_embedder

    return ChunkIndex(exam_id, get_embedder())


# --- supervisor ------------------------------------------------------------------------------

def make_services(pack: ExamPack, ledger: Ledger):
    """Supervisor 그래프용 실제 에이전트: 색인이 있으면 검색, 연습 문제 문체 은행, Bedrock 모델."""
    from misslens.agents.collector import Crawl4AIFetcher, load_sources
    from misslens.agents.generator.context import RetrievalContext
    from misslens.agents.ingest import Ingestor
    from misslens.graph import Services
    from misslens.llm import get_llm
    from misslens.practice import PracticeSamples, load_bank

    index = open_index(pack.exam_id)
    bank = load_bank(pack.exam_id)
    samples = PracticeSamples(bank) if bank else None
    context = RetrievalContext(index) if index is not None and len(index) else None

    def make_generator() -> Generator:
        return Generator(pack, ledger, context=context, samples=samples,
                         copy_guard=samples.copy_reason if samples else None)

    try:
        sources = load_sources(pack.exam_id)
    except FileNotFoundError:
        sources = None
    return Services(pack=pack, ledger=ledger, make_generator=make_generator, router_llm=get_llm("router"), index=index,
                    planner_llm=get_llm("planner"),
                    make_ingestor=lambda: Ingestor(pack, ledger, open_index(pack.exam_id, must_exist=True)),
                    sources=sources, fetchers={n: Crawl4AIFetcher(n) for n in (sources.sites if sources else {})})


def run_supervisor(cli: "CLI", request: dict, *, explainer: Any = None, svc: Any = None,
                   checkpointer: Any = None) -> dict:
    """요청 하나를 Supervisor 그래프로 보내고, 그 interrupt(계획 승인, 로그인 대기)에는 여기서 답한다."""
    from langgraph.types import Command

    from misslens.agents.collector import login, new_thread_id, sqlite_checkpointer
    from misslens.agents.collector.graph import run_config
    from misslens.graph import build_supervisor_graph

    svc = svc or make_services(cli.pack, cli.ledger)
    cli.index = svc.index
    graph = build_supervisor_graph(svc, checkpointer=checkpointer or sqlite_checkpointer())
    cfg = run_config(new_thread_id("supervisor"))

    def open_login(payload: dict) -> None:
        login(payload["site"], payload["login_url"])

    out = graph.invoke(request, cfg)
    while out.get("__interrupt__"):
        payload = out["__interrupt__"][0].value
        if payload["kind"] == "plan":
            resume: Any = cli.ask_plan(payload)
        else:
            resume = login_prompt(cli.console, cli._ask, lambda p=payload: open_login(p))(payload)
        out = graph.invoke(Command(resume=resume), cfg)
    if out.get("message"):
        cli.console.print(escape(out["message"]))
    if out.get("report"):
        cli.render_report(out["report"])
    if out.get("results"):
        print_collect_summary(cli.console, out)
    for r in out.get("ingested") or []:
        cli.console.print(f"{r['status']} {r['chunks']}개 청크 · {escape(r['source'])}")
    if out.get("set_id") is not None and out["set_id"] in svc.streams:
        n = sum(out["plan"]["counts"].values()) if out.get("plan") and out.get("plan_status") == "approved" \
            else int((out.get("args") or {}).get("count") or 10)
        cli.run_stream(out["set_id"], n, svc.streams.pop(out["set_id"]), explainer=explainer)
    return out


# --- 수집기(collector) -----------------------------------------------------------------------------

STATUS_LABEL = {"saved": "[green]저장[/]", "expanded": "[cyan]레슨 목록[/]", "empty": "[yellow]본문 없음[/]",
                "not_enrolled": "[yellow]수강 등록 필요[/]", "error": "[red]오류[/]", "blocked": "[red]차단[/]"}


def login_prompt(console: Console, ask: Ask, open_login: Callable[[], Any]) -> Callable[[dict], str]:
    """CLI용 interrupt② 처리기: 상황을 설명하고, 학습자가 로그인하게 한 뒤, resume 값을 돌려준다."""

    def on_login(payload: dict) -> str:
        console.print(Panel(f"세션이 만료됐거나 로그인이 필요합니다.\n사이트: {escape(payload['site'])}\n"
                            f"멈춘 페이지: {escape(payload['url'])}\n로그인 주소: {escape(payload['login_url'])}",
                            title=f"로그인 대기 ({payload.get('attempt', 1)}회째)", border_style="yellow"))
        while True:
            a = ask("y=로그인 창 열기, n=이미 로그인함(바로 재개), q=수집 중단 > ").strip().lower()
            if a in ("y", "yes", ""):
                open_login()
                return "done"
            if a in ("n", "no"):
                return "done"
            if a in ("q", "quit"):
                return "abort"

    return on_login


def print_collect_summary(console: Console, state: dict) -> list[str]:
    """URL마다 한 줄을 출력하고, 저장한 파일 경로를 돌려준다."""
    saved: list[str] = []
    counts: dict[str, int] = {}
    for r in state.get("results", []):
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "saved":
            saved.append(r["path"])
        else:
            console.print(f"{STATUS_LABEL.get(r['status'], r['status'])} {escape(r['url'])} "
                          f"{escape(r.get('note') or '')}")
    console.print("수집 결과: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "없음"))
    if state.get("stopped"):
        console.print(f"[red]{escape(state['stopped'])}[/]")
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="misslens")
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quiz", help="세트 풀이 → 제출 → 리뷰 → 진단")
    q.add_argument("-n", type=int, default=5)
    q.add_argument("--exam", default="ccar-f")
    q.add_argument("--no-explain", action="store_true", help="LLM 해설 대신 저장된 해설만 보여 준다")
    s = sub.add_parser("status", help="현황 요약")
    s.add_argument("--exam", default="ccar-f")
    g = sub.add_parser("ingest", help="자료 넣기(PDF/MD/TXT/HTML 파일이나 폴더, --paste면 표준 입력)")
    g.add_argument("paths", nargs="*")
    g.add_argument("--paste", metavar="LABEL", help="표준 입력의 텍스트를 이 이름으로 넣는다")
    g.add_argument("--exam", default="ccar-f")
    pl = sub.add_parser("plan", help="오늘의 계획 제안 → 승인(interrupt①) → 세트 풀이")
    pl.add_argument("-n", type=int, default=10)
    pl.add_argument("--exam", default="ccar-f")
    pl.add_argument("--no-explain", action="store_true")
    ak = sub.add_parser("ask", help="자연어 지시(예: \"도구 설계 영역 5문제 내줘\")")
    ak.add_argument("text")
    ak.add_argument("--exam", default="ccar-f")
    ak.add_argument("--no-explain", action="store_true")
    rv = sub.add_parser("retrieval-eval", help="검색 품질 점수·색인 건강 검사, 기준선 대비 회귀 확인(D50)")
    rv.add_argument("--save-baseline", action="store_true", help="현재 점수를 새 기준선으로 저장")
    rv.add_argument("--max-drop", type=float, default=0.05, help="기준선보다 이만큼 넘게 떨어지면 실패")
    rv.add_argument("--json", metavar="PATH", help="전체 보고서를 JSON으로 저장")
    rv.add_argument("--exam", default="ccar-f")
    ev = sub.add_parser("eval", help="진단 일치율(내 판단 vs 판정) 보기, --export로 eval 데이터 저장")
    ev.add_argument("--export", nargs="?", const="", metavar="PATH",
                    help="기본값 data/eval/misses-<날짜>.yaml (git 제외)")
    ev.add_argument("--exam", default="ccar-f")
    xq = sub.add_parser("export-questions", help="생성된 문제를 상위 모델 검토용 Markdown으로 내보내기(D58)")
    xq.add_argument("--set", type=int, action="append", dest="sets", metavar="ID",
                    help="이 세트만(여러 번 지정 가능). 생략하면 제출한 세트 전부")
    xq.add_argument("--evidence", choices=["full", "sources", "none"], default="full",
                    help="근거 부록: full=발췌 포함(기본), sources=출처 이름만, none=넣지 않음")
    xq.add_argument("-o", "--output", metavar="PATH", help="기본값 data/review/questions-<날짜>.md (샌드박스는 data/sandbox/review/)")
    xq.add_argument("--exam", default="ccar-f")
    qe = sub.add_parser("question-eval", help="문제 생성 품질 라운드 평가: 인-아웃 세트 생성 → LLM 판정 → 리포트(D59)")
    qe.add_argument("--round", type=int, required=True, dest="round_no", help="라운드 번호(리포트 파일 이름)")
    qe.add_argument("--existing", action="store_true",
                    help="새로 만들지 않고 세트 파일의 1라운드 문제(round1_source)를 판정한다")
    qe.add_argument("--from-set", type=int, metavar="ID",
                    help="새로 만들지 않고 평가용 DB의 이 세트를 다시 판정한다(판정기를 바꾼 뒤 재채점)")
    qe.add_argument("--relabel", action="store_true",
                    help="모델 호출 없이, 저장된 판정 결과에 세트 파일의 외부 라벨을 다시 붙여 지표·리포트를 다시 만든다")
    qe.add_argument("--limit", type=int, help="앞에서 N개 세부 항목만(시험 실행용)")
    qe.add_argument("--notes", default="", help="이번 라운드에서 바꾼 것(리포트에 그대로 들어감)")
    qe.add_argument("--exam", default="ccar-f")
    qq = sub.add_parser("query", help="질문 하나에 근거 기반으로 답하기(POST /query와 같은 경로, D61)")
    qq.add_argument("question")
    qq.add_argument("--json", action="store_true", help="API 응답과 같은 JSON으로 출력")
    qq.add_argument("--exam", default="ccar-f")
    qv = sub.add_parser("query-eval", help="test_queries.csv 인-아웃 세트 통과율 + RAGAS 지표 라운드 평가(D61, 실호출)")
    qv.add_argument("--round", type=int, required=True, dest="round_no", help="라운드 번호(리포트 파일 이름)")
    qv.add_argument("--notes", default="", help="이번 라운드에서 바꾼 것(리포트에 그대로 들어감)")
    qv.add_argument("--limit", type=int, help="앞에서 N건만(시험 실행용)")
    qv.add_argument("--no-ragas", action="store_true", help="RAGAS 지표를 건너뛴다(통과율만)")
    qv.add_argument("--rejudge", action="store_true",
                    help="새로 묻지 않고 저장된 라운드의 답변을 지금 판정기로 다시 채점한다(판정기를 바꾼 뒤)")
    qv.add_argument("--exam", default="ccar-f")
    tr = sub.add_parser("trace", help="로컬 트레이스 요약: 역할별 모델 호출·노드 소요 시간·최근 실행(D60)")
    tr.add_argument("--day", help="YYYYMMDD(기본 오늘) 또는 all")
    tr.add_argument("--last", type=int, default=3, help="최근 실행 N개를 단계별로 보여 준다")
    tr.add_argument("--exam", default="ccar-f")
    pr = sub.add_parser("practice", help="연습 문제 파일을 문체 예시 은행으로 변환")
    pr.add_argument("action", choices=["import"])
    pr.add_argument("dir", nargs="?", help="기본값 data/sources/practice_problems")
    pr.add_argument("--exam", default="ccar-f")
    co = sub.add_parser("collect", help="로그인 세션으로 sources.yaml의 페이지 수집")
    co.add_argument("action", choices=["login", "run", "reindex"])
    co.add_argument("site", nargs="?", help="sources.yaml의 사이트 이름(생략하면 전부)")
    co.add_argument("--thread", help="이어서 실행할 thread_id(로그인 대기에서 멈춘 실행)")
    co.add_argument("--limit", type=int, help="처음 N개 소스만(시험 실행용)")
    co.add_argument("--no-ingest", action="store_true", help="저장만 하고 색인에 넣지 않는다")
    co.add_argument("--exam", default="ccar-f")
    r = sub.add_parser("search", help="검색 결과 확인")
    r.add_argument("query")
    r.add_argument("--task", help="세부 항목 ID로 태그 우선")
    r.add_argument("-k", type=int, default=6)
    r.add_argument("--exam", default="ccar-f")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    from misslens.paths import SandboxRefused, ledger_path, refuse_in_sandbox, sandbox

    pack = load_exam_pack(args.exam)
    if args.cmd in ("ingest", "collect") or (args.cmd == "practice"):
        try:
            refuse_in_sandbox({"ingest": "자료 색인", "collect": "교안 수집", "practice": "연습 문제 은행 갱신"}[args.cmd])
        except SandboxRefused as exc:
            Console().print(f"[red]{exc}[/]")
            return 2
    ledger = Ledger()
    cli = CLI(pack, ledger)
    if sandbox():
        cli.console.print(f"[magenta]샌드박스 모드[/]: 학습 기록·체크포인트는 {ledger_path().parent}에 따로 저장됩니다.")
    try:
        if args.cmd == "status":
            cli.status()
        elif args.cmd == "ingest":
            from misslens.agents.ingest import Ingestor, UnsupportedFile, from_paste, load_file

            docs = []
            if args.paste:
                docs.append(from_paste(sys.stdin.read(), args.paste))
            for path in expand_paths(args.paths):
                try:
                    docs.append(load_file(path))
                except (UnsupportedFile, OSError) as exc:
                    cli.console.print(f"[red]건너뜀[/] {escape(str(exc))}")
            if not docs:
                cli.console.print("넣을 자료가 없습니다.")
                return 1
            run_ingest(Ingestor(pack, ledger, open_index(pack.exam_id, must_exist=True)), docs, cli.console)
        elif args.cmd in ("plan", "ask"):
            from misslens.llm import get_llm

            explainer = None if args.no_explain else get_llm("explainer")
            request = ({"intent": "plan_quiz", "args": {"count": args.n}} if args.cmd == "plan"
                       else {"text": args.text})
            run_supervisor(cli, request, explainer=explainer)
        elif args.cmd == "retrieval-eval":
            return retrieval_eval_command(cli, args)
        elif args.cmd == "eval":
            eval_command(cli, args.export)
        elif args.cmd == "export-questions":
            return export_questions_command(cli, args)
        elif args.cmd == "question-eval":
            return question_eval_command(cli, args)
        elif args.cmd == "trace":
            return trace_command(cli, args)
        elif args.cmd == "query":
            return query_command(cli, args)
        elif args.cmd == "query-eval":
            return query_eval_command(cli, args)
        elif args.cmd == "practice":
            from misslens.practice import SOURCE_DIR, parse_dir, save_bank, summarize

            bank = parse_dir(Path(args.dir) if args.dir else SOURCE_DIR, valid_task_ids=set(pack.task_ids))
            path = save_bank(bank, pack.exam_id)
            s = summarize(bank)
            thin = [t for t in pack.task_ids if s["usable_by_task"].get(t, 0) < 2]
            cli.console.print(f"{s['total']}문항 → {path}\n문체 예시로 사용 {s['usable']}개 (제외: 중복 {s['duplicates']}, "
                              f"정답·해설 불일치 {s['unconfirmed']}, 주의 표시 {s['disputed']})\n"
                              f"정답 분포 {s['answer_letters']}")
            if thin:
                cli.console.print("예시가 2개 미만인 세부 항목: " + ", ".join(thin))
        elif args.cmd == "collect":
            return collect_command(args, pack, ledger, cli)
        elif args.cmd == "search":
            index = open_index(pack.exam_id)
            if index is None:
                cli.console.print("색인이 없습니다. 먼저 `ingest`로 자료를 넣으세요.")
                return 1
            for h in index.search(args.query, k=args.k, task_id=args.task):
                tag = "태그 일치" if h.tagged else ",".join(h.task_ids) or "미분류"
                cli.console.print(Panel(escape(h.text[:600]), title=f"{escape(h.source)} · {tag} · {h.score:.4f}"))
        else:
            from misslens.agents.generator.context import RetrievalContext
            from misslens.llm import get_llm

            index = open_index(pack.exam_id)
            if index is None or len(index) == 0:
                cli.console.print("[yellow]자료 색인이 없어 세부 항목 설명만으로 출제합니다. 사실 검증이 약하니 "
                                  "먼저 `ingest`로 자료를 넣으세요(D34).[/]")
                context = None
            else:
                context = RetrievalContext(index)
                cli.index = index
            from misslens.practice import PracticeSamples, load_bank

            bank = load_bank(pack.exam_id)
            samples = PracticeSamples(bank) if bank else None
            explainer = None if args.no_explain else get_llm("explainer")
            cli.quiz(args.n, Generator(pack, ledger, context=context, samples=samples,
                                       copy_guard=samples.copy_reason if samples else None), explainer=explainer)
    except (KeyboardInterrupt, EOFError):
        cli.console.print("\n중단했습니다. 제출하지 않은 세트는 통계에 들어가지 않습니다.")
        return 130
    finally:
        ledger.close()
    return 0


def render_retrieval_report(console: Console, rep: dict) -> None:
    g, tc, h = rep["golden"], rep["task_context"], rep["health"]
    tbl = Table("모드", "hit@1", "hit@3", "hit@5", "hit@10", "MRR@10", "상위5 자료원", "상위5 중복쌍", "5위 밖",
                title=f"정답 질의 {g['answerable']}/{g['items']}개(답할 수 있는 것만 채점)")
    labels = {"plain": "일반(자유 질문)", "coach": "코치(태그 ≤2)", "task": "출제(태그 우선)"}
    for mode, m in g["modes"].items():
        tbl.add_row(labels.get(mode, mode), *(f"{m[k]:.2f}" for k in ("hit@1", "hit@3", "hit@5", "hit@10", "mrr@10")),
                    f"{m['top5_sources']:.1f}", str(m["top5_dup_pairs"]), ", ".join(m["misses"]) or "-")
    console.print(tbl)
    by = g["modes"].get("plain", {}).get("by_domain", {})
    if by:
        console.print("영역별 hit@5(일반): " + " · ".join(f"{d} {v['hit@5']:.2f}({v['n']})" for d, v in by.items()))
    if g["unanswerable"]:
        console.print(f"[yellow]색인에 정답이 없는 질의[/]: {', '.join(g['unanswerable'])}")
    console.print(f"출제 근거(세부 항목 {tc['tasks']}개): 상위 6개 중 태그 일치 평균 {tc['avg_tagged_share']:.0%}, "
                  f"자료원 평균 {tc['avg_source_groups']:.1f}종, 태그 일치 50% 미만: {', '.join(tc['tasks_below_half_tagged']) or '없음'}")
    marks = " · ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in h["checks"].items())
    console.print(f"색인 건강: 청크 {h['chunks']}, 잡음 {h['junk_chunks']}, 짧은 청크 {h['short_chunks']}, "
                  f"완전 중복 {h['exact_duplicate_chunks']}, 미분류 {h['unassigned_ratio']:.0%}, "
                  f"자료 적은 항목 {', '.join(h['thin_tasks']) or '없음'}\n{marks}")


def retrieval_eval_command(cli: CLI, args) -> int:
    from misslens.agents.collector import load_sources
    from misslens.retrieval.evaluate import evaluate, load_baseline, load_golden, save_baseline

    index = open_index(cli.pack.exam_id)
    if index is None:
        cli.console.print("색인이 없습니다. 먼저 `ingest`로 자료를 넣으세요.")
        return 1
    try:
        rules = list(load_sources(cli.pack.exam_id).sites.values())
    except FileNotFoundError:
        rules = []
    baseline = load_baseline(cli.pack.exam_id)
    rep = evaluate(index, cli.pack, load_golden(cli.pack.exam_id), rules, baseline, args.max_drop)
    d = rep.as_dict()
    render_retrieval_report(cli.console, d)
    if args.json:
        Path(args.json).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.save_baseline:
        if not d["health"]["ok"]:
            cli.console.print("[red]색인 건강 검사가 실패해 기준선을 저장하지 않았습니다.[/]")
            return 1
        cli.console.print(f"기준선을 저장했습니다: {save_baseline(rep)}")
        return 0
    if baseline is None:
        cli.console.print("[yellow]기준선이 없습니다. 결과가 괜찮으면 --save-baseline으로 저장하세요.[/]")
    if rep.regressions:
        cli.console.print("[red]회귀:[/] " + "; ".join(rep.regressions))
        return 1
    cli.console.print("[green]기준선 대비 회귀 없음[/]" if baseline else "")
    return 0


def export_eval(ledger: Ledger, pack: ExamPack, path: Path) -> int:
    """학습자가 직접 라벨을 붙인 오답을 tests/fixtures/eval_misses.yaml 형식으로 내보낸다(T10 eval, D43)."""
    import yaml

    items = []
    for m in ledger.labeled_misses(pack.exam_id):
        q = m["payload"]
        items.append({
            "id": f"a{m['attempt_id']}", "is_decoy": False, "domain": m["domain"], "task_id": m["task_id"],
            "stem_en": q["stem_en"], "options_en": [o["text_en"] for o in q["options"]],
            "answer_index": q["answer_index"], "key_constraints": q["key_constraints"],
            "violates": [o["violates"] for o in q["options"]], "first_choice": m["chosen_index"],
            "self_label": {"miss_type": m["learner_label"], "missed_phrase": None, "stage": m["stage"]},
            "system": {"miss_type": m["miss_type"], "missed_phrase": m["missed_phrase"]},
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "exam_id": pack.exam_id, "collected_at": datetime.now().strftime("%Y-%m-%d"),
           "source": "MissLens review self-check (label given after the answer, before the explanation)",
           "items": items}
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return len(items)


def eval_command(cli: CLI, export: str | None) -> None:
    from misslens.bedrock import PROJECT_ROOT
    from misslens.ledger import diagnosis_agreement

    ag = diagnosis_agreement(cli.ledger, cli.pack.exam_id)
    rate = "-" if ag.rate is None else f"{ag.rate:.0%}"
    cli.console.print(f"판단을 남긴 오답 {ag.labeled}개 · 비교 가능 {ag.compared}개 · 일치 {ag.agree}개 · 일치율 {rate}")
    if ag.confusion:
        tbl = Table("판정 → 내 판단", "개수")
        for k, v in sorted(ag.confusion.items()):
            tbl.add_row(k, str(v))
        cli.console.print(tbl)
    if export is not None:
        path = Path(export) if export else PROJECT_ROOT / "data" / "eval" / f"misses-{datetime.now():%Y%m%d}.yaml"
        n = export_eval(cli.ledger, cli.pack, path)
        cli.console.print(f"{n}개를 저장했습니다: {path}")


def export_questions_command(cli: CLI, args: argparse.Namespace) -> int:
    """제출한 세트의 문제를 검토 지시문·근거 부록과 함께 Markdown 파일로 저장한다(D58). 모델 호출 없음."""
    from misslens.agents.generator.context import RetrievalContext
    from misslens.paths import state_dir
    from misslens.review_export import build_review_markdown, collect_questions

    if not collect_questions(cli.ledger, cli.pack.exam_id, args.sets):
        cli.console.print("내보낼 문제가 없습니다. 제출한 세트(제출·리뷰 완료)의 문제만 내보냅니다.")
        return 1
    context = None
    if args.evidence != "none":
        index = open_index(cli.pack.exam_id)
        context = RetrievalContext(index) if index is not None and len(index) else None
    out = build_review_markdown(cli.pack, cli.ledger, set_ids=args.sets, evidence=args.evidence, context=context)
    path = Path(args.output) if args.output else state_dir() / "review" / f"questions-{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.markdown, encoding="utf-8")
    cli.console.print(f"{out.questions}문제(세트 {', '.join(map(str, out.sets))}, 세부 항목 {len(out.tasks)}개)를 "
                      f"저장했습니다: {path}\n파일을 Claude Opus나 GPT 대화에 첨부하거나 붙여 넣으세요. "
                      "검토 지시문이 파일 맨 위에 있습니다.")
    return 0


def question_eval_command(cli: CLI, args: argparse.Namespace) -> int:
    """인-아웃 세트로 문제를 만들고(또는 1라운드 문제를 읽고) 판정기로 채점해 라운드 리포트를 저장한다(D59)."""
    from misslens.agents.generator.context import RetrievalContext
    from misslens.bedrock import PROJECT_ROOT, load_model_config
    from misslens.llm import get_llm
    from misslens.practice import PracticeSamples, load_bank
    from misslens.question_eval import (ROUNDS_DB, SetItem, generate_round, judge_all, load_question_set,
                                        load_round_metrics, metrics, relabel_round, render_report, save_round)
    from misslens.review_export import build_review_markdown

    pack = cli.pack
    items, meta = load_question_set(pack.exam_id)
    items = items[:args.limit] if args.limit else items
    if args.relabel:
        m, results, notes = relabel_round(args.round_no, items)
        notes = args.notes or notes or "-"
        report = render_report(args.round_no, m, results, previous=load_round_metrics(args.round_no - 1),
                               notes=notes, judge_model=load_model_config().roles["judge"]["model"])
        path = save_round(args.round_no, m, results, report, notes=notes)
        c = m.get("calibration") or {}
        cli.console.print(f"외부 라벨 {c.get('compared', 0)}문제로 다시 계산(모델 호출 없음) · 판정기와 OK/비OK 일치 "
                          f"{c.get('ok_vs_not_ok_agreement')} → {path}")
        return 0
    index = open_index(pack.exam_id)
    context = RetrievalContext(index) if index is not None and len(index) else None
    say = lambda m: cli.console.print(escape(m))  # noqa: E731
    gen_stats = None
    if args.existing:
        src = Ledger(PROJECT_ROOT / meta["round1_source"])
        by_id = {q["id"]: q for s in src.list_sets(pack.exam_id, limit=10_000) for q in src.set_questions(s["id"])}
        questions = [(f"r{args.round_no}-q{it.round1_qid}", it, by_id[it.round1_qid]["payload"])
                     for it in items if it.round1_qid in by_id]
    elif args.from_set is not None:
        rounds = Ledger(ROUNDS_DB)
        made = {q["task_id"]: q for q in rounds.set_questions(args.from_set) if q["status"] == "ready"}
        questions = [(f"r{args.round_no}-q{n + 1}", SetItem(it.task_id, external=it.labels.get(args.round_no)),
                      made[it.task_id]["payload"]) for n, it in enumerate(items) if it.task_id in made]
        set_id = args.from_set
    else:
        ROUNDS_DB.parent.mkdir(parents=True, exist_ok=True)
        rounds = Ledger(ROUNDS_DB)
        bank = load_bank(pack.exam_id)
        samples = PracticeSamples(bank) if bank else None
        gen = Generator(pack, rounds, context=context, samples=samples,
                        copy_guard=samples.copy_reason if samples else None)
        say(f"{len(items)}개 세부 항목으로 문제 생성 중(평가용 DB: {ROUNDS_DB})")
        set_id, gen_stats = generate_round(gen, rounds, pack, [SetItem(i.task_id) for i in items], progress=say)
        made = {q["task_id"]: q for q in rounds.set_questions(set_id) if q["status"] == "ready"}
        questions = [(f"r{args.round_no}-q{n + 1}", SetItem(it.task_id), made[it.task_id]["payload"])
                     for n, it in enumerate(items) if it.task_id in made]
        md = build_review_markdown(pack, rounds, set_ids=[set_id], context=context, statuses=("generating",))
        export = ROUNDS_DB.parent / f"round{args.round_no}_questions.md"
        export.write_text(md.markdown, encoding="utf-8")
        say(f"외부 검토용 파일: {export}")
    judge_model = load_model_config().roles["judge"]["model"]
    say(f"판정 중: {len(questions)}문제 · {judge_model}")
    results = judge_all(get_llm("judge"), pack, questions, context, progress=say)
    m = metrics(results, gen_stats)
    if not args.existing:
        m["rounds_db_set"] = set_id
    if args.from_set is not None:          # 다시 채점: 같은 라운드의 생성 통계는 이전 기록에서 이어받는다
        before = load_round_metrics(args.round_no) or {}
        if before.get("generation"):
            m["generation"] = before["generation"]
    report = render_report(args.round_no, m, results, previous=load_round_metrics(args.round_no - 1),
                           notes=args.notes or "-", judge_model=judge_model)
    path = save_round(args.round_no, m, results, report, notes=args.notes or "-")
    cli.console.print(f"판정기 OK {m['judge_ok_rate']} · violates 유효 {m['violates_valid_rate']} · "
                      f"정답 하나 {m['single_answer_rate']} → {path}")
    return 0


def _qa_service(cli: CLI):
    from misslens.api import QAService
    from misslens.llm import get_llm

    index = open_index(cli.pack.exam_id)
    cli.index = index
    return QAService(cli.pack, cli.ledger, index, get_llm("explainer"), get_llm("router"))


def query_command(cli: CLI, args: argparse.Namespace) -> int:
    """질문 하나 → 답변·근거·단계(API와 같은 answer_query)."""
    res = _qa_service(cli).ask(args.question)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
        return 0
    cli.console.print(Panel(escape(res.answer), title=f"답변 · {res.route}"))
    for s in res.trace:
        cli.console.print(f"[dim]{escape(s['step'])}[/] {escape(s['input'][:80])} → {escape(s['output'][:120])}")
    if res.contexts:
        cli.console.print("근거: " + ", ".join(sorted({escape(c["doc_id"]) for c in res.contexts})))
    return 0


def query_eval_command(cli: CLI, args: argparse.Namespace) -> int:
    """test_queries.csv로 질의 API를 평가하고 evaluation/query_round<N>_report.md를 저장한다(D61)."""
    from misslens.bedrock import load_model_config
    from misslens.llm import get_llm
    from misslens.query_eval import (load_queries, load_round_metrics, metrics, ragas_scores, render_report,
                                     run_round, save_round)

    cases = load_queries()
    cases = cases[:args.limit] if args.limit else cases
    judge = get_llm("judge")
    say = lambda m: cli.console.print(escape(m))  # noqa: E731
    if args.rejudge:
        return query_rejudge(cli, args, cases, judge, say)
    svc = _qa_service(cli)
    say(f"{len(cases)}건 실행·판정 중")
    rows = run_round(cases, svc.ask, judge, progress=say)
    if not args.no_ragas:
        from misslens.retrieval.embed import get_embedder

        say("RAGAS 지표 계산 중(context_precision·faithfulness·answer_relevancy)")
        ragas_scores(rows, judge, get_embedder())
    m = metrics(rows)
    roles = load_model_config().roles
    notes = args.notes or "-"
    report = render_report(args.round_no, m, rows, previous=load_round_metrics(args.round_no - 1), notes=notes,
                           judge_model=roles["judge"]["model"], answer_model=roles["explainer"]["model"])
    path = save_round(args.round_no, m, rows, report, notes=notes)
    r = m["ragas"]
    cli.console.print(f"통과 {m['passed']}/{m['total']} · RAGAS cp {r['context_precision']} · f {r['faithfulness']} · "
                      f"ar {r['answer_relevancy']} ({m['ragas_samples']}건) → {path}")
    return 0


def query_rejudge(cli: CLI, args: argparse.Namespace, cases: list, judge: Any, say: Callable[[str], None]) -> int:
    """판정기를 바꾼 뒤 앞 라운드를 같은 판정기로 다시 채점한다. 답변·도구 호출·RAGAS는 저장본 그대로이고,
    저장본에 학습 기록 근거가 없으면(판정기 v1 라운드) 같은 ledger에서 같은 도구로 다시 계산한다."""
    import re
    import shutil

    from misslens.agents.grader.coach import make_tools
    from misslens.bedrock import load_model_config
    from misslens.qa import status_text
    from misslens.query_eval import REPORT_DIR, load_round_metrics, metrics, rejudge_round, render_report, save_round

    pack, ledger = cli.pack, cli.ledger
    history = next(t for t in make_tools(pack, ledger, None) if t.name == "learner_history")

    def evidence_for(case, saved: dict) -> list[dict]:
        ev = []
        if "build_report" in saved["steps"]:
            ev.append({"step": "build_report", "input": pack.exam_id, "output": status_text(pack, ledger)})
        if "learner_history" in saved["steps"]:
            # "2.3에서"처럼 한글이 바로 붙어도 찾는다(\b는 한글을 단어 문자로 본다)
            for tid in re.findall(r"(?<![\d.])\d\.\d(?![\d.])", case.input):
                ev.append({"step": "learner_history", "input": json.dumps({"task_id": tid}),
                           "output": history.invoke({"task_id": tid})})
        return ev

    for suffix, v1 in ((".json", f"query_round{args.round_no}_judge_v1.json"),
                       ("_report.md", f"query_round{args.round_no}_judge_v1_report.md")):
        src = REPORT_DIR / f"query_round{args.round_no}{suffix}"
        if src.exists() and not (REPORT_DIR / v1).exists():
            shutil.copy(src, REPORT_DIR / v1)          # 판정기 v1 결과는 따로 남긴다
    rows, old_notes = rejudge_round(args.round_no, cases, judge, evidence_for, progress=say)
    m = metrics(rows)
    notes = (args.notes or old_notes.split(" · 판정기 v")[0]) + (
        " · 판정기 v2로 다시 채점(답변·도구 호출·RAGAS는 저장본, 학습 기록 근거는 같은 ledger에서 다시 계산)")
    roles = load_model_config().roles
    report = render_report(args.round_no, m, rows, previous=load_round_metrics(args.round_no - 1), notes=notes,
                           judge_model=roles["judge"]["model"], answer_model=roles["explainer"]["model"])
    path = save_round(args.round_no, m, rows, report, notes=notes)
    cli.console.print(f"다시 채점: 통과 {m['passed']}/{m['total']} → {path}")
    return 0


def trace_command(cli: CLI, args: argparse.Namespace) -> int:
    """로컬 트레이스(data/traces/, 샌드박스는 data/sandbox/traces/)를 요약한다. 모델 호출 없음."""
    from misslens.trace import load_events, recent_traces, summarize, trace_dir

    events = load_events(args.day)
    if not events:
        cli.console.print(f"트레이스가 없습니다: {trace_dir()} ({args.day or '오늘'}). 앱이나 CLI로 모델을 호출하면 쌓입니다.")
        return 1
    s = summarize(events)
    tbl = Table("역할", "호출", "오류", "전환", "p50 ms", "p95 ms", "입력 토큰", "출력 토큰", "엔드포인트")
    for role, r in sorted(s["roles"].items()):
        tbl.add_row(role, str(r["calls"]), str(r["errors"]), str(r["failovers"]), str(r["p50_ms"]),
                    str(r["p95_ms"]), f"{r['in']:,}", f"{r['out']:,}",
                    ", ".join(f"{k} {v}" for k, v in sorted(r["endpoints"].items())))
    cli.console.print(tbl)
    if s["nodes"]:
        nt = Table("그래프 노드", "실행", "오류", "interrupt", "p50 ms", "p95 ms")
        for node, n in sorted(s["nodes"].items()):
            nt.add_row(node, str(n["runs"]), str(n["errors"]), str(n["interrupts"]), str(n["p50_ms"]),
                       str(n["p95_ms"]))
        cli.console.print(nt)
    for tid, evs in recent_traces(events, args.last):
        lines = []
        for e in evs:
            where = e.get("node") or e.get("span") or ""
            if e["kind"] == "llm":
                tok = f" {e.get('input_tokens') or '-'}→{e.get('output_tokens') or '-'} tok"
                fo = f" 전환 {e['failovers']}" if e.get("failovers") else ""
                lines.append(f"  llm  {e['role']:<9} {e['status']:<10} {e['ms']:>6} ms{tok}{fo} "
                             f"{e.get('endpoint') or ''} {('@' + where) if where else ''}")
            else:
                label = e.get("node") if e["kind"] == "node" else e.get("name")
                extra = " ".join(f"{k}={e[k]}" for k in ("task_id", "tries", "items") if e.get(k) is not None)
                lines.append(f"  {e['kind']:<4} {label:<14} {e['status']:<10} {e['ms']:>6} ms {extra}")
        cli.console.print(Panel(escape("\n".join(lines)), title=f"trace {tid} · {evs[0]['ts'][11:19]}"))
    cli.console.print(f"실행 {s['traces']}개 · 파일 {trace_dir()}")
    return 0


def reindex_collected(cli: CLI, pack: ExamPack, ledger: Ledger, srcs: Any, names: list[str]) -> int:
    """저장된 페이지를 사이트의 현재 규칙으로 다시 정리하고, 색인에서 그 chunk를 교체한다(D48).
    크롤링하지 않는다: data/sources/collected/<site>/를 대상으로 한다. 영상만 있는 페이지(본문이 남지 않음)는 색인에서 빠진다."""
    from misslens.agents.collector.graph import COLLECTED_DIR, MIN_TEXT_CHARS, clean_markdown, save_page
    from misslens.agents.ingest import Document, Ingestor
    from misslens.agents.ingest.loaders import load_file

    index = open_index(pack.exam_id, must_exist=True)
    docs: list[Any] = []
    for n in names:
        site = srcs.sites[n]
        files = sorted((COLLECTED_DIR / n).glob("*.md"))
        uris, empty = [], 0
        for f in files:
            raw = load_file(f)                          # 표시 주석은 제거됨, source = 페이지 URL
            body = "\n".join(l for l in raw.text.splitlines() if not l.startswith("<!-- collected"))
            text = clean_markdown(site.strip_chrome(body))
            uris.append(raw.source)
            if len(text.strip()) < MIN_TEXT_CHARS:
                empty += 1
                continue
            save_page(n, raw.source, text, COLLECTED_DIR)
            docs.append(Document(text=text, source=raw.source, source_type="collect"))
        removed = index.remove(lambda c, u=set(uris): c.source in u)
        index.save()
        ledger.remove_materials(pack.exam_id, uris)
        cli.console.print(f"{n}: 파일 {len(files)}개 · 색인에서 뺀 청크 {removed}개 · 본문 없는(영상만) 페이지 {empty}개")
    run_ingest(Ingestor(pack, ledger, index), docs, cli.console)
    return 0


def collect_command(args, pack: ExamPack, ledger: Ledger, cli: CLI) -> int:
    from dataclasses import replace

    from misslens.agents.collector import (
        Crawl4AIFetcher, load_sources, login, new_thread_id, run_collect, sqlite_checkpointer,
    )

    srcs = load_sources(pack.exam_id)
    names = [args.site] if args.site else list(srcs.sites)
    unknown = [n for n in names if n not in srcs.sites]
    if unknown:
        cli.console.print(f"sources.yaml에 없는 사이트: {', '.join(unknown)}")
        return 1
    if args.action == "reindex":
        return reindex_collected(cli, pack, ledger, srcs, names)
    if args.action == "login":
        for n in names:
            path = login(n, srcs.sites[n].login_url)
            cli.console.print(f"로그인 프로필을 저장했습니다: {path} (git 제외, 권한 0700)")
        return 0

    saved: list[str] = []
    checkpointer = sqlite_checkpointer()
    for n in names:
        site, sources = srcs.sites[n], srcs.for_site(n)[: args.limit or None]
        thread_id = args.thread or new_thread_id(n)
        cli.console.print(f"[bold]{n}[/] 수집 시작: 소스 {len(sources)}개, thread_id={thread_id}")
        on_login = login_prompt(cli.console, cli._ask, lambda n=n: login(n, srcs.sites[n].login_url))
        state = run_collect(site, sources, Crawl4AIFetcher(n), on_login, checkpointer=checkpointer,
                            thread_id=thread_id)
        saved += print_collect_summary(cli.console, state)
    if saved and not args.no_ingest:
        from misslens.agents.ingest import Ingestor, load_file

        docs = [replace(load_file(p), source_type="collect") for p in saved]
        run_ingest(Ingestor(pack, ledger, open_index(pack.exam_id, must_exist=True)), docs, cli.console)
    return 0


if __name__ == "__main__":
    sys.exit(main())
