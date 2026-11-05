"""Collector: sources.yaml 규칙, 강의 펼치기, 안전장치, interrupt② 로그인 대기와 재개(TASK T7)."""

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
    """각본대로 응답하는 페이지. `expire_once` URL은 처음 가져올 때 로그인 리다이렉트로 응답한다."""

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


# --- 강의 펼치기 ---------------------------------------------------------------------------


def test_lesson_links_one_level_same_course_only(srcs):
    links = course_pages()[f"{HOST}/course-a"].links
    assert lesson_links(f"{HOST}/course-a", links, srcs.sites["academy"]) == [
        f"{HOST}/course-a/101", f"{HOST}/course-a/102"]


# --- 그래프 --------------------------------------------------------------------------------------


def test_collects_lessons_saves_text_and_reports_empty(srcs, tmp_path):
    f = FakeFetcher(course_pages())
    clock = Clock()
    g = graph(srcs, f, tmp_path, clock)
    out = g.invoke(initial_state(srcs.sites["academy"], srcs.sources), run_config("t1"))
    assert f.fetched == [f"{HOST}/course-a", f"{HOST}/course-a/101", f"{HOST}/course-a/102", f"{HOST}/page/notes"]
    status = {r["url"]: r["status"] for r in out["results"]}
    assert status == {f"{HOST}/course-a": "expanded", f"{HOST}/course-a/101": "saved",
                      f"{HOST}/course-a/102": "empty", f"{HOST}/page/notes": "saved"}
    assert clock.slept == [2, 2, 2]  # 요청 사이에는 최소 delay_seconds, 첫 요청 전에는 없음
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
    assert f.closed == 1  # 학습자가 로그인하기 전에 headless 브라우저를 해제한다
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
    assert len(f.fetched) == 4  # 첫 시도 + 로그인 후 재시도 3회


def test_run_collect_survives_process_restart_at_login_wait(srcs, tmp_path):
    """로그인 프롬프트에서 Ctrl-C: 새 프로세스가 SQLite checkpoint에서 같은 thread_id를 재개한다."""
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
    assert f2.fetched == [f"{HOST}/course-a/102", f"{HOST}/page/notes"]  # 두 번 가져온 것은 없다
    assert [r["status"] for r in out["results"]] == ["expanded", "saved", "empty", "saved"]


def test_save_page_keeps_file_when_content_unchanged(tmp_path):
    p = save_page("academy", f"{HOST}/course-a/101", BODY, tmp_path)
    first = p.read_text(encoding="utf-8")
    assert save_page("academy", f"{HOST}/course-a/101", BODY + "\n", tmp_path).read_text(encoding="utf-8") == first
    save_page("academy", f"{HOST}/course-a/101", BODY + " changed", tmp_path)
    assert "changed" in p.read_text(encoding="utf-8")


# --- CLI 부분 ---------------------------------------------------------------------------------


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
    """Skilljar는 리다이렉트하는 대신 Sign In 링크가 있는 로그아웃 상태의 강좌 페이지를 보여 준다."""
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


# --- 페이지 장식과 재색인(D48) ---------------------------------------------------------------

PLAYER = """## Lesson title
Open in Claude
Copy notes
1 minute, 25 seconds of 5 minutes, 44 secondsVolume 90%
Keyboard ShortcutsEnabledDisabled
WhiteBlackRedGreenBlueYellowMagentaCyan
This video is still being processed. Please check back later and refresh the page.
Uh oh! Something went wrong, please try again.
Real lesson text about tool_choice that must stay.
"""


def test_strip_chrome_drops_menus_and_player_block():
    site = load_sources("ccar-f").sites["skilljar"]
    out = site.strip_chrome(PLAYER)
    assert out.splitlines() == ["## Lesson title", "Real lesson text about tool_choice that must stay."]


def test_strip_chrome_keeps_text_when_block_never_ends():
    site = load_sources("ccar-f").sites["skilljar"]
    text = "0 of 1 minute, 25 secondsVolume 90%\nbody without an end marker"
    assert site.strip_chrome(text) == text


def test_reindex_replaces_collected_chunks(tmp_path, monkeypatch):
    import io

    from rich.console import Console

    import misslens.agents.collector.graph as cg
    import misslens.cli as cli_mod
    import misslens.llm as llm_mod
    from fakes import FakeRoleLLM
    from misslens.cli import CLI, reindex_collected
    from misslens.exam_pack import load_exam_pack
    from misslens.ledger import Ledger
    from misslens.retrieval import ChunkIndex, IndexedChunk
    from test_retrieval import FakeEmbedder

    pack = load_exam_pack("ccar-f")
    srcs = load_sources("ccar-f")
    monkeypatch.setattr(cg, "COLLECTED_DIR", tmp_path / "collected")
    url_text, url_video = f"https://anthropic-partners.skilljar.com/c/1", f"https://anthropic-partners.skilljar.com/c/2"
    save_page("skilljar", url_text, PLAYER + "More real text. " * 20, tmp_path / "collected")
    save_page("skilljar", url_video, PLAYER.replace("Real lesson text about tool_choice that must stay.", ""),
              tmp_path / "collected")
    index = ChunkIndex("ccar-f", FakeEmbedder(), root=tmp_path / "index")
    index.add([IndexedChunk("guide text about tool_choice", "guide.pdf", "g", ["2.3"]),
               IndexedChunk("WhiteBlackRed junk " + url_text, url_text, "old1", []),
               IndexedChunk("old video chunk", url_video, "old2", [])])
    ledger = Ledger(":memory:")
    ledger.add_material("ccar-f", source_type="collect", content_hash="old1", source_uri=url_text)
    ledger.add_material("ccar-f", source_type="collect", content_hash="old2", source_uri=url_video)
    monkeypatch.setattr(cli_mod, "open_index", lambda exam_id, must_exist=False: index)
    monkeypatch.setattr(llm_mod, "get_llm", lambda role: FakeRoleLLM([{"items": [{"index": 0, "task_ids": ["2.3"]}]}]))
    out = io.StringIO()
    reindex_collected(CLI(pack, ledger, console=Console(file=out, width=200)), pack, ledger, srcs, ["skilljar"])

    sources = [c.source for c in index.chunks]
    assert "guide.pdf" in sources and url_video not in sources          # 가이드는 남고, 동영상만 있는 페이지는 사라진다
    assert all("WhiteBlackRed" not in c.text for c in index.chunks)
    assert any(c.source == url_text and "must stay" in c.text for c in index.chunks)
    rows = [tuple(r) for r in ledger._query("SELECT source_uri, content_hash FROM materials")]
    assert rows and all(h not in ("old1", "old2") for _, h in rows)
    assert "본문 없는(영상만) 페이지 1개" in out.getvalue()
    ledger.close()
