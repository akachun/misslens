"""MissLens CLI (core loop without Streamlit).

    python -m misslens.cli quiz [-n 5] [--exam ccar-f]   # set -> solve -> submit -> re-ask -> diagnosis
    python -m misslens.cli status [--exam ccar-f]        # quick numbers from the ledger

No interrupts here (CLAUDE.md rule 4): solving and review are plain prompts.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from misslens.agents.generator import Generator, QuestionStream
from misslens.agents.grader import build_reask_form, diagnosis_rows, explain_miss, highlight, submit_review
from misslens.exam_pack import ExamPack, load_exam_pack
from misslens.ledger import (
    Answer,
    Ledger,
    avg_time,
    coverage,
    generation_failures,
    miss_breakdown,
    set_score,
    top_missed_phrases,
)

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
        c = self.console
        set_id = self.ledger.create_set(self.pack.exam_id, n)
        stream = QuestionStream(generator, set_id, n).start()
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
        self.show_diagnosis(set_id, explainer=explainer)

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
            text = explain_miss(explainer, self.pack, r) if explainer is not None else q["explanation_native"]
            extra = f"\n\n놓친 조건: [bold yellow]{escape(r.missed_phrase)}[/]" if r.missed_phrase else ""
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
        c, ex = self.console, self.pack.exam_id
        cov = coverage(self.ledger, ex, self.pack.task_ids, task_id_map=self.pack.task_id_map)
        mb = miss_breakdown(self.ledger, ex)
        t = avg_time(self.ledger, ex)
        ratio = "-" if mb.reading_ratio is None else f"{mb.reading_ratio:.0%}"
        base = "-" if mb.baseline is None else f"{mb.baseline:.0%}"
        c.print(f"범위 {len(cov.asked)}/{len(self.pack.task_ids)} ({cov.ratio:.0%}) · 해석 실수 비율 {ratio} "
                f"(미끼 기준선 {base}) · 개념 {mb.concept} · 해석 {mb.reading} · 불확실 {mb.uncertain}")
        if t.overall_ms is not None:
            c.print(f"평균 풀이 시간 {t.overall_ms / 1000:.0f}초")
        phrases = top_missed_phrases(self.ledger, ex)
        if phrases:
            tbl = Table("놓친 문구", "횟수")
            for p, n in phrases:
                tbl.add_row(escape(p), str(n))
            c.print(tbl)
        if cov.never_asked:
            c.print("미출제: " + ", ".join(cov.never_asked))
        fails = generation_failures(self.ledger, ex, limit=5)
        for f in fails:
            c.print(f"[red]생성 실패[/] {f['task_id']} ({f['tries']}회): {escape(f['reason'][:160])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="misslens")
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quiz", help="세트 풀이 → 제출 → 리뷰 → 진단")
    q.add_argument("-n", type=int, default=5)
    q.add_argument("--exam", default="ccar-f")
    q.add_argument("--no-explain", action="store_true", help="LLM 해설 대신 저장된 해설만 보여 준다")
    s = sub.add_parser("status", help="현황 요약")
    s.add_argument("--exam", default="ccar-f")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    pack = load_exam_pack(args.exam)
    ledger = Ledger()
    cli = CLI(pack, ledger)
    try:
        if args.cmd == "status":
            cli.status()
        else:
            from misslens.llm import get_llm

            explainer = None if args.no_explain else get_llm("explainer")
            cli.quiz(args.n, Generator(pack, ledger), explainer=explainer)
    except (KeyboardInterrupt, EOFError):
        cli.console.print("\n중단했습니다. 제출하지 않은 세트는 통계에 들어가지 않습니다.")
        return 130
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
