"""MissLens CLI (core loop without Streamlit).

    python -m misslens.cli quiz [-n 5] [--exam ccar-f]   # set -> solve -> submit -> re-ask -> diagnosis
    python -m misslens.cli status [--exam ccar-f]        # quick numbers from the ledger
    python -m misslens.cli ingest PATH... [--paste]      # add study material (files or folders) to the index
    python -m misslens.cli search "query" [--task 2.1]   # inspect what retrieval returns
    python -m misslens.cli plan [-n 10]                  # Planner draft -> approve (interrupt①) -> solve
    python -m misslens.cli ask "도구 설계 5문제"          # free text -> Supervisor router -> agent
    python -m misslens.cli eval [--export]               # diagnosis vs the learner's own labels (T10 eval)
    python -m misslens.cli practice import [DIR]         # parse learner-supplied practice sets into the style bank
    python -m misslens.cli collect login skilljar        # sign in by hand in a visible browser (profile only)
    python -m misslens.cli collect run [--site skilljar] # collect sources.yaml pages, then ingest them

Interrupts exist only in `collect run` (login wait, interrupt②) and later in the Planner (CLAUDE.md rule 4).

Solving and review are plain prompts, never interrupts.
"""

from __future__ import annotations

import argparse
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
        self.index = None   # ChunkIndex for the coach's search tool (D44); None = coach searches nothing

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

    # --- quiz ------------------------------------------------------------------------------

    def quiz(self, n: int, generator: Generator, *, explainer: Any = None) -> int | None:
        set_id = self.ledger.create_set(self.pack.exam_id, n)
        stream = QuestionStream(generator, set_id, n).start()
        return self.run_stream(set_id, n, stream, explainer=explainer)

    def run_stream(self, set_id: int, n: int, stream: QuestionStream, *, explainer: Any = None) -> int | None:
        """Solve a set while it is being generated, submit it, then review (no interrupts, rule 4)."""
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
                    # Number by seq so solving, diagnosis and '문제 이상' use the same numbers.
                    answers.append(self._solve(ev.seq + 1, ev.question_id, ev.question))
                elif ev.kind == "failed":
                    c.print(f"[red]문제 {ev.seq + 1} 생성 실패[/]: {escape(ev.message)}")
                else:
                    c.print(("[yellow]" if ev.kind == "stopped" else "[green]") + escape(ev.message) + "[/]")
                    break
        except (KeyboardInterrupt, EOFError):
            generator.cancel()          # stop writing to the ledger before main() closes it
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

    # --- review ----------------------------------------------------------------------------

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
        """Learner's own label per miss, after the answer but before the verdict and explanation (D43).
        Builds the diagnosis eval data while studying. Returns how many labels were saved."""
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

    # --- status ----------------------------------------------------------------------------

    def status(self) -> None:
        from misslens.agents.reporter import build_report

        self.render_report(build_report(self.pack, self.ledger).as_dict())

    def render_report(self, r: dict) -> None:
        """One renderer for the Reporter's numbers (CLI status and the Supervisor's status route)."""
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

    # --- plan approval (interrupt①) ---------------------------------------------------------

    def ask_plan(self, payload: dict) -> dict:
        """Show the drafted plan; return the resume value for the planner's interrupt."""
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


# --- material ------------------------------------------------------------------------------

def expand_paths(paths: list[str]) -> list[Path]:
    """Files as given, and supported files inside folders (sorted, recursive)."""
    from misslens.agents.ingest.loaders import SUPPORTED

    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED))
        else:
            out.append(p)
    return out


def run_ingest(ingestor: Any, docs: list[Any], console: Console) -> int:
    """Ingest documents and print one line each plus the per-task material counts. Returns chunks added."""
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
    """The exam's index, or None if nothing was ingested yet (avoids loading bge-m3 for nothing)."""
    from misslens.retrieval.index import INDEX_ROOT, ChunkIndex

    if not (INDEX_ROOT / exam_id / "meta.json").exists() and not must_exist:
        return None
    from misslens.retrieval.embed import get_embedder

    return ChunkIndex(exam_id, get_embedder())


# --- supervisor ------------------------------------------------------------------------------

def make_services(pack: ExamPack, ledger: Ledger):
    """Real agents for the Supervisor graph: retrieval if indexed, practice style bank, Bedrock models."""
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
    """Send one request through the Supervisor graph; answer its interrupts (plan approval, login wait) here."""
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


# --- collector -----------------------------------------------------------------------------

STATUS_LABEL = {"saved": "[green]저장[/]", "expanded": "[cyan]레슨 목록[/]", "empty": "[yellow]본문 없음[/]",
                "not_enrolled": "[yellow]수강 등록 필요[/]", "error": "[red]오류[/]", "blocked": "[red]차단[/]"}


def login_prompt(console: Console, ask: Ask, open_login: Callable[[], Any]) -> Callable[[dict], str]:
    """interrupt② handler for the CLI: explain, let the learner log in, return the resume value."""

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
    """One line per URL; returns the saved file paths."""
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
    ev = sub.add_parser("eval", help="진단 일치율(내 판단 vs 판정) 보기, --export로 eval 데이터 저장")
    ev.add_argument("--export", nargs="?", const="", metavar="PATH",
                    help="기본값 data/eval/misses-<날짜>.yaml (git 제외)")
    ev.add_argument("--exam", default="ccar-f")
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
        elif args.cmd == "eval":
            eval_command(cli, args.export)
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


def export_eval(ledger: Ledger, pack: ExamPack, path: Path) -> int:
    """Misses with the learner's own label, in the shape of tests/fixtures/eval_misses.yaml (T10 eval, D43)."""
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


def reindex_collected(cli: CLI, pack: ExamPack, ledger: Ledger, srcs: Any, names: list[str]) -> int:
    """Re-clean saved pages with the site's current rules and replace their chunks in the index (D48).
    No crawling: works on data/sources/collected/<site>/. Video-only pages (no text left) leave the index."""
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
            raw = load_file(f)                          # marker stripped, source = page URL
            body = "\n".join(l for l in raw.text.splitlines() if not l.startswith("<!-- collected"))
            text = clean_markdown(site.strip_chrome(body))
            uris.append(raw.source)
            if len(text.strip()) < MIN_TEXT_CHARS:
                empty += 1
                continue
            save_page(n, raw.source, text)
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
