"""Collector: sources.yaml rules, lesson expansion, safety, interrupt② login wait and resume (TASK T7)."""

from __future__ import annotations

import io

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from rich.console import Console

from misslens.agents.collector import (
    FetchResult, SourceList, build_collector_graph, initial_state, lesson_links, load_sources, run_collect,
    save_page, sqlite_checkpointer,
)
from misslens.agents.collector.graph import run_config
from misslens.agents.ingest import load_file
from misslens.cli import login_prompt, print_collect_summary

HOST = "https://academy.example.com"
BODY = "Hooks run before and after tool calls. PreToolUse can block a call. " * 10

RAW = {
    "sites": {"academy": {"login_url": f"{HOST}/auth/login", "allowed_domains": ["academy.example.com"],
                          "expired_markers": ["/auth/login"], "not_enrolled_markers": ["/checkout/"],
                          "delay_seconds": 2}},
    "sources": [{"site": "academy", "url": f"{HOST}/course-a", "expand": "lessons", "title": "A"},
                {"site": "academy", "url": f"{HOST}/page/notes"}],
}


@pytest.fixture
def srcs():
    return SourceList.model_validate(RAW)


class FakeFetcher:
    """Scripted pages. `expire_once` URLs answer with the login redirect the first time they are fetched."""

    def __init__(self, pages: dict[str, FetchResult], expire_once: set[str] = frozenset()):
        self.pages = pages
        self.expire_once = set(expire_once)
        self.fetched: list[str] = []
        self.closed = 0

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.expire_once:
            self.expire_once.discard(url)
            return FetchResult(url, f"{HOST}/auth/login?next=/x", 200, "Sign in")
        return self.pages.get(url) or FetchResult(url, url, 404, "", error="not found")

    def close(self):
        self.closed += 1


def page(url, text=BODY, links=()):
    return FetchResult(url, url, 200, text, list(links))


def course_pages():
    return {
        f"{HOST}/course-a": page(f"{HOST}/course-a", "Course A overview", [
            f"{HOST}/course-a/101", "/course-a/102#top", f"{HOST}/course-a/101", f"{HOST}/course-a/103/quiz",
            f"{HOST}/course-a/logout", f"{HOST}/other/1", "https://evil.example.org/course-a/9",
            f"{HOST}/course-a/104?x=1"]),
        f"{HOST}/course-a/101": page(f"{HOST}/course-a/101"),
        f"{HOST}/course-a/102": page(f"{HOST}/course-a/102", "video only"),
        f"{HOST}/page/notes": page(f"{HOST}/page/notes"),
    }


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def graph(srcs, fetcher, tmp_path, clock=None, checkpointer=None):
    clock = clock or Clock()
    return build_collector_graph(srcs.sites["academy"], fetcher, checkpointer=checkpointer or InMemorySaver(),
                                 out_dir=tmp_path, sleep=clock.sleep, clock=clock)


# --- sources.yaml ------------------------------------------------------------------------------


def test_ccar_f_sources_load_and_stay_on_allowed_domain():
    s = load_sources("ccar-f")
    assert len(s.for_site("skilljar")) == 7
    assert all(x.expand == "lessons" for x in s.sources)
    assert s.sites["skilljar"].delay_seconds >= 2


@pytest.mark.parametrize("mutate", [
    lambda r: r["sources"].append({"site": "academy", "url": "https://elsewhere.example.org/x"}),
    lambda r: r["sources"].append({"site": "academy", "url": f"http://academy.example.com/x"}),
    lambda r: r["sources"].append({"site": "nope", "url": f"{HOST}/x"}),
    lambda r: r["sites"]["academy"].update(delay_seconds=0.5),
    lambda r: r["sources"][0].update(expand="everything"),
])
def test_invalid_sources_rejected(mutate):
    import copy

    raw = copy.deepcopy(RAW)
    mutate(raw)
    with pytest.raises(ValueError):
        SourceList.model_validate(raw)


# --- lesson expansion ---------------------------------------------------------------------------


def test_lesson_links_one_level_same_course_only(srcs):
    links = course_pages()[f"{HOST}/course-a"].links
    assert lesson_links(f"{HOST}/course-a", links, srcs.sites["academy"]) == [
        f"{HOST}/course-a/101", f"{HOST}/course-a/102"]


# --- graph --------------------------------------------------------------------------------------


def test_collects_lessons_saves_text_and_reports_empty(srcs, tmp_path):
    f = FakeFetcher(course_pages())
    clock = Clock()
    g = graph(srcs, f, tmp_path, clock)
    out = g.invoke(initial_state(srcs.sites["academy"], srcs.sources), run_config("t1"))
    assert f.fetched == [f"{HOST}/course-a", f"{HOST}/course-a/101", f"{HOST}/course-a/102", f"{HOST}/page/notes"]
    status = {r["url"]: r["status"] for r in out["results"]}
    assert status == {f"{HOST}/course-a": "expanded", f"{HOST}/course-a/101": "saved",
                      f"{HOST}/course-a/102": "empty", f"{HOST}/page/notes": "saved"}
    assert clock.slept == [2, 2, 2]  # at least delay_seconds between requests, none before the first
    doc = load_file(next(r["path"] for r in out["results"] if r["status"] == "saved"))
    assert doc.source == f"{HOST}/course-a/101" and "PreToolUse" in doc.text


def test_not_enrolled_and_errors_are_reported_and_skipped(srcs, tmp_path):
    pages = {f"{HOST}/course-a": FetchResult(f"{HOST}/course-a", f"{HOST}/checkout/abc", 200, "Enroll")}
    out = graph(srcs, FakeFetcher(pages), tmp_path).invoke(
        initial_state(srcs.sites["academy"], srcs.sources), run_config("t2"))
    assert [r["status"] for r in out["results"]] == ["not_enrolled", "error"]


def test_expired_session_interrupts_then_resumes_at_same_url(srcs, tmp_path):
    f = FakeFetcher(course_pages(), expire_once={f"{HOST}/course-a/101"})
    g = graph(srcs, f, tmp_path)
    cfg = run_config("t3")
    out = g.invoke(initial_state(srcs.sites["academy"], srcs.sources), cfg)
    payload = out["__interrupt__"][0].value
    assert payload == {"kind": "login", "site": "academy", "login_url": f"{HOST}/auth/login",
                       "url": f"{HOST}/course-a/101", "attempt": 1}
    assert f.closed == 1  # headless browser released before the learner logs in
    out = g.invoke(Command(resume="done"), cfg)
    assert "__interrupt__" not in out
    assert f.fetched.count(f"{HOST}/course-a/101") == 2
    assert [r["status"] for r in out["results"]] == ["expanded", "saved", "empty", "saved"]


def test_abort_at_login_wait_stops_run(srcs, tmp_path):
    f = FakeFetcher(course_pages(), expire_once={f"{HOST}/course-a"})
    g = graph(srcs, f, tmp_path)
    cfg = run_config("t4")
    g.invoke(initial_state(srcs.sites["academy"], srcs.sources), cfg)
    out = g.invoke(Command(resume="abort"), cfg)
    assert out["queue"] == [] and "중단" in out["stopped"]
    assert f.fetched == [f"{HOST}/course-a"]


def test_login_that_never_sticks_stops_after_max_waits(srcs, tmp_path):
    class AlwaysExpired(FakeFetcher):
        def fetch(self, url):
            self.fetched.append(url)
            return FetchResult(url, f"{HOST}/auth/login", 200, "")

    f = AlwaysExpired({})
    out = run_collect(srcs.sites["academy"], srcs.sources, f, lambda p: "done", checkpointer=InMemorySaver(),
                      thread_id="t5", out_dir=tmp_path, sleep=lambda s: None)
    assert "세션이 확인되지 않아" in out["stopped"]
    assert len(f.fetched) == 4  # first try + 3 retries after login


def test_run_collect_survives_process_restart_at_login_wait(srcs, tmp_path):
    """Ctrl-C at the login prompt: a new process resumes the same thread_id from the SQLite checkpoint."""
    db = tmp_path / "checkpoints.sqlite"

    class Quit(Exception):
        pass

    def quit_at_prompt(payload):
        raise Quit

    f1 = FakeFetcher(course_pages(), expire_once={f"{HOST}/course-a/102"})
    with pytest.raises(Quit):
        run_collect(srcs.sites["academy"], srcs.sources, f1, quit_at_prompt, checkpointer=sqlite_checkpointer(db),
                    thread_id="t6", out_dir=tmp_path, sleep=lambda s: None)
    assert f1.closed >= 1

    f2 = FakeFetcher(course_pages())
    out = run_collect(srcs.sites["academy"], srcs.sources, f2, lambda p: "done", checkpointer=sqlite_checkpointer(db),
                      thread_id="t6", out_dir=tmp_path, sleep=lambda s: None)
    assert f2.fetched == [f"{HOST}/course-a/102", f"{HOST}/page/notes"]  # nothing fetched twice
    assert [r["status"] for r in out["results"]] == ["expanded", "saved", "empty", "saved"]


def test_save_page_keeps_file_when_content_unchanged(tmp_path):
    p = save_page("academy", f"{HOST}/course-a/101", BODY, tmp_path)
    first = p.read_text(encoding="utf-8")
    assert save_page("academy", f"{HOST}/course-a/101", BODY + "\n", tmp_path).read_text(encoding="utf-8") == first
    save_page("academy", f"{HOST}/course-a/101", BODY + " changed", tmp_path)
    assert "changed" in p.read_text(encoding="utf-8")


# --- CLI pieces ---------------------------------------------------------------------------------


def test_login_prompt_answers():
    opened = []
    console = Console(file=io.StringIO())
    payload = {"site": "academy", "url": f"{HOST}/x", "login_url": f"{HOST}/auth/login", "attempt": 1}
    answers = iter(["huh", "y"])
    assert login_prompt(console, lambda _: next(answers), lambda: opened.append(1))(payload) == "done"
    assert opened == [1]
    assert login_prompt(console, lambda _: "n", lambda: opened.append(1))(payload) == "done" and opened == [1]
    assert login_prompt(console, lambda _: "q", lambda: None)(payload) == "abort"


def test_summary_lists_problems_and_returns_saved_paths():
    out = io.StringIO()
    saved = print_collect_summary(Console(file=out, width=200), {"results": [
        {"url": "u1", "status": "saved", "path": "/p1"}, {"url": "u2", "status": "not_enrolled", "note": "등록"}],
        "stopped": None})
    assert saved == ["/p1"]
    assert "수강 등록 필요" in out.getvalue() and "saved 1" in out.getvalue()


def test_logged_out_course_page_triggers_login_wait(srcs, tmp_path):
    """Skilljar shows a logged-out course page with a Sign In link instead of redirecting."""
    pages = course_pages()
    pages[f"{HOST}/course-a"] = page(f"{HOST}/course-a", "Course A", [f"{HOST}/auth/login?next=%2Fcourse-a"])
    out = graph(srcs, FakeFetcher(pages), tmp_path).invoke(
        initial_state(srcs.sites["academy"], srcs.sources), run_config("t7"))
    assert out["__interrupt__"][0].value["url"] == f"{HOST}/course-a"


def test_course_page_with_enroll_link_only_is_not_enrolled(srcs, tmp_path):
    pages = course_pages()
    pages[f"{HOST}/course-a"] = page(f"{HOST}/course-a", "Course A", [f"{HOST}/checkout/abc"])
    out = graph(srcs, FakeFetcher(pages), tmp_path).invoke(
        initial_state(srcs.sites["academy"], srcs.sources), run_config("t8"))
    assert [r["status"] for r in out["results"]] == ["not_enrolled", "saved"]


def test_clean_markdown_drops_menus_keeps_prose():
    from misslens.agents.collector.graph import clean_markdown

    md = ("[ Home ](https://x/) [ Courses ](https://x/c)\n![logo](https://x/l.png)\n"
          "Use [hooks](https://x/h) to block a tool call before it runs.\n```bash\nclaude -p \"hi\"\n```")
    out = clean_markdown(md)
    assert "Home" not in out and "logo" not in out
    assert "Use [hooks](https://x/h) to block" in out and 'claude -p "hi"' in out
