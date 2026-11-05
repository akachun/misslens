"""Collector subgraph: read listed pages with the learner's login profile (ARCHITECTURE §4.4, §9).

    START -> fetch --(queue left)--> fetch
                   --(session expired)--> wait_login --interrupt②--> fetch (same URL again)
                   --(queue empty)--> END

interrupt② is the ONLY interrupt here (CLAUDE.md rule 4). wait_login closes the headless browser first so the
learner can open the same profile in a visible window, then pauses the graph until resumed with
Command(resume="done") or Command(resume="abort"). The run's state lives in the checkpointer
(data/checkpoints.sqlite), so a resume picks up from the URL that hit the login wall.

Pages are saved to data/sources/collected/<site>/ wrapped in the "untrusted external content" marker, so the
Ingest loader keeps their URL as the source and the text is treated as reference data, never instructions.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urldefrag, urljoin, urlparse

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from misslens.agents.collector.browser import Fetcher
from misslens.agents.collector.sources import Site, Source
from misslens.agents.ingest.loaders import content_hash
from misslens.bedrock import PROJECT_ROOT

logger = logging.getLogger(__name__)

COLLECTED_DIR = PROJECT_ROOT / "data" / "sources" / "collected"
MIN_TEXT_CHARS = 200          # less than this after extraction = treat as empty (§9: report failure)
MAX_LOGIN_WAITS = 3           # consecutive login waits for one URL before the run stops
_SKIP_PATH = ("logout", "signout", "sign_out", "delete", "remove", "cancel", "unsubscribe", "checkout", "purchase")


class CollectState(TypedDict, total=False):
    site: str
    queue: list[dict]       # {"url", "expand", "title"}; queue[0] is the URL in progress
    seen: list[str]
    results: list[dict]     # {"url", "status", "path"?, "chars"?, "note"?}
    need_login: bool
    login_waits: int
    stopped: str | None


def initial_state(site: Site, sources: list[Source]) -> CollectState:
    return {"site": site.name, "queue": [s.model_dump(include={"url", "expand", "title"}) for s in sources],
            "seen": [], "results": [], "need_login": False, "login_waits": 0, "stopped": None}


def lesson_links(course_url: str, links: list[str], site: Site) -> list[str]:
    """Links one path segment below the course page on the same host (D38). Order kept, duplicates removed."""
    base = urlparse(course_url)
    prefix = base.path.rstrip("/") + "/"
    out: list[str] = []
    for href in links:
        url = urldefrag(urljoin(course_url, href))[0]
        u = urlparse(url)
        rest = u.path[len(prefix):] if u.path.startswith(prefix) else ""
        if (not site.allows(url) or u.hostname != base.hostname or not rest or "/" in rest.strip("/")
                or any(k in u.path.lower() for k in _SKIP_PATH) or u.query):
            continue
        url = url.rstrip("/")
        if url not in out:
            out.append(url)
    return out


_MD_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def clean_markdown(text: str) -> str:
    """Drop menu-like lines (two or more links and little else) and image-only lines; keep prose and code."""
    out: list[str] = []
    for line in text.splitlines():
        links = _MD_LINK.findall(line)
        rest = _MD_LINK.sub("", line).strip(" \t|*-•·>")
        if links and (len(links) >= 2 and len(rest) < 40 or not rest):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _slug(url: str) -> str:
    path = urlparse(url).path.strip("/") or "index"
    return "".join(c if c.isalnum() or c in "-_" else "__" for c in path)[:150]


def save_page(site: str, url: str, text: str, root: Path = COLLECTED_DIR) -> Path:
    d = root / site
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(url)}.md"
    if path.exists():
        old_body = path.read_text(encoding="utf-8").splitlines()[2:-1]  # drop marker, timestamp, end marker
        if content_hash("\n".join(old_body)) == content_hash(text):
            return path  # same content: keep the file (and its first-collected time)
    path.write_text(f"--- BEGIN UNTRUSTED EXTERNAL CONTENT (source: {url}) ---\n"
                    f"<!-- collected {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n"
                    f"{text.strip()}\n--- END UNTRUSTED EXTERNAL CONTENT ---\n", encoding="utf-8")
    return path


RECURSION_LIMIT = 2000  # one graph step per page; LangGraph's default (25) would stop a course midway


def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def build_collector_graph(site: Site, fetcher: Fetcher, *, checkpointer: Any = None,
                          out_dir: Path | None = None, sleep: Callable[[float], None] | None = None,
                          clock: Callable[[], float] | None = None):
    # Defaults resolved at call time (not import time) so tests can patch the module.
    out_dir = out_dir or COLLECTED_DIR
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    last_request = [float("-inf")]

    def fetch(state: CollectState) -> CollectState:
        queue = list(state.get("queue", []))
        if not queue:
            return {}
        item, rest = queue[0], queue[1:]
        url = item["url"]
        results = list(state.get("results", []))
        seen = list(state.get("seen", []))
        if not site.allows(url):
            results.append({"url": url, "status": "blocked", "note": "허용 도메인 밖"})
            return {"queue": rest, "results": results}

        wait = site.delay_seconds - (clock() - last_request[0])
        if wait > 0:
            sleep(wait)
        r = fetcher.fetch(url)
        last_request[0] = clock()

        if site.is_expired(r.final_url, r.status):
            logger.info("session expired at %s (final %s, status %s)", url, r.final_url, r.status)
            return {"need_login": True}
        update: CollectState = {"login_waits": 0, "seen": seen + [url]}
        if site.is_not_enrolled(r.final_url):
            results.append({"url": url, "status": "not_enrolled", "note": "수강 등록이 필요합니다"})
        elif r.error:
            results.append({"url": url, "status": "error", "note": r.error})
        elif item.get("expand") == "lessons":
            known = set(seen) | {q["url"] for q in rest}
            lessons = [u for u in lesson_links(url, r.links, site) if u not in known]
            if not lessons and not lesson_links(url, r.links, site):
                # A logged-out course page does not redirect; it just shows "Sign In" and no lessons.
                if any(site.is_expired(urljoin(url, h), None) for h in r.links):
                    return {"need_login": True}
                if any(site.is_not_enrolled(urljoin(url, h)) for h in r.links):
                    results.append({"url": url, "status": "not_enrolled", "note": "수강 등록이 필요합니다"})
                    update.update(queue=rest, results=results)
                    return update
            results.append({"url": url, "status": "expanded", "chars": len(lessons),
                            "note": f"레슨 {len(lessons)}개" + ("" if lessons else " (로그인·수강 등록 상태 확인)")})
            rest = [{"url": u, "expand": None, "title": item.get("title")} for u in lessons] + rest
        elif len(text := clean_markdown(r.markdown)) < MIN_TEXT_CHARS:
            results.append({"url": url, "status": "empty", "chars": len(text),
                            "note": "본문이 거의 없습니다(영상만 있는 레슨이거나 추출 실패)"})
        else:
            path = save_page(site.name, url, text, out_dir)
            results.append({"url": url, "status": "saved", "path": str(path), "chars": len(text)})
        update.update(queue=rest, results=results)
        return update

    def wait_login(state: CollectState) -> CollectState:
        fetcher.close()  # free the profile for the visible login window; idempotent on re-execution
        waits = state.get("login_waits", 0) + 1
        url = state["queue"][0]["url"]
        if waits > MAX_LOGIN_WAITS:
            return {"need_login": False, "queue": [], "login_waits": waits,
                    "stopped": f"로그인 후에도 세션이 확인되지 않아 중단했습니다({url})"}
        answer = interrupt({"kind": "login", "site": site.name, "login_url": site.login_url, "url": url,
                            "attempt": waits})
        if answer == "abort":
            return {"need_login": False, "queue": [], "login_waits": waits, "stopped": "사용자가 수집을 중단했습니다"}
        return {"need_login": False, "login_waits": waits}

    def route(state: CollectState) -> str:
        if state.get("need_login"):
            return "wait_login"
        return "fetch" if state.get("queue") else END

    g = StateGraph(CollectState)
    g.add_node("fetch", fetch)
    g.add_node("wait_login", wait_login)
    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", route, ["fetch", "wait_login", END])
    g.add_conditional_edges("wait_login", lambda s: "fetch" if s.get("queue") else END, ["fetch", END])
    return g.compile(checkpointer=checkpointer)
