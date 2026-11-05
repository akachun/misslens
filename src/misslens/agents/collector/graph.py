"""Collector subgraph: 학습자의 로그인 프로필로 목록에 있는 페이지를 읽는다(ARCHITECTURE §4.4, §9).

    START -> fetch --(queue left)--> fetch
                   --(session expired)--> wait_login --interrupt②--> fetch (same URL again)
                   --(queue empty)--> END

여기서 interrupt는 interrupt② 하나뿐이다(CLAUDE.md 규칙 4). wait_login은 먼저 headless 브라우저를 닫아
학습자가 같은 프로필을 보이는 창에서 열 수 있게 한 뒤, Command(resume="done") 또는 Command(resume="abort")로
재개될 때까지 그래프를 멈춘다. 실행 상태는 checkpointer(data/checkpoints.sqlite)에 있으므로 재개하면
로그인 벽에 막힌 URL부터 다시 시작한다.

페이지는 "untrusted external content" 표시로 감싸 data/sources/collected/<site>/에 저장한다. 그래서 Ingest
loader가 URL을 source로 유지하고, 본문은 지시가 아니라 참고 자료로만 다뤄진다.
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
MIN_TEXT_CHARS = 200          # 추출 후 이보다 짧으면 빈 페이지로 본다(§9: 실패로 보고)
MAX_LOGIN_WAITS = 3           # 한 URL에서 연속 로그인 대기가 이 횟수를 넘으면 실행을 멈춘다
_SKIP_PATH = ("logout", "signout", "sign_out", "delete", "remove", "cancel", "unsubscribe", "checkout", "purchase")


class CollectState(TypedDict, total=False):
    site: str
    queue: list[dict]       # {"url", "expand", "title"}; queue[0]이 처리 중인 URL
    seen: list[str]
    results: list[dict]     # {"url", "status", "path"?, "chars"?, "note"?}
    need_login: bool
    login_waits: int
    stopped: str | None


def initial_state(site: Site, sources: list[Source]) -> CollectState:
    return {"site": site.name, "queue": [s.model_dump(include={"url", "expand", "title"}) for s in sources],
            "seen": [], "results": [], "need_login": False, "login_waits": 0, "stopped": None}


def lesson_links(course_url: str, links: list[str], site: Site) -> list[str]:
    """같은 호스트에서 코스 페이지 바로 아래 경로 한 단계의 링크(D38). 순서는 유지하고 중복은 없앤다."""
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
    """메뉴 같은 줄(링크가 둘 이상이고 다른 내용이 거의 없음)과 이미지만 있는 줄은 지우고, 본문과 코드는 남긴다."""
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


def save_page(site: str, url: str, text: str, root: Path | None = None) -> Path:
    # 호출 시점에 정한다. import 시점에 기본값을 묶어 두었다가 테스트가 실제 폴더에 쓴 적이 있다.
    d = (root or COLLECTED_DIR) / site
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(url)}.md"
    if path.exists():
        old_body = path.read_text(encoding="utf-8").splitlines()[2:-1]  # 시작 표시, 시각, 끝 표시를 뺀다
        if content_hash("\n".join(old_body)) == content_hash(text):
            return path  # 내용이 같으면 파일(과 처음 수집한 시각)을 그대로 둔다
    path.write_text(f"--- BEGIN UNTRUSTED EXTERNAL CONTENT (source: {url}) ---\n"
                    f"<!-- collected {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n"
                    f"{text.strip()}\n--- END UNTRUSTED EXTERNAL CONTENT ---\n", encoding="utf-8")
    return path


RECURSION_LIMIT = 2000  # 페이지마다 그래프 한 단계. LangGraph 기본값(25)이면 코스 중간에 멈춘다


def run_config(thread_id: str) -> dict:
    """그래프 실행 설정. CLI Supervisor와 수집이 함께 쓴다. 노드 트레이스 콜백을 붙인다(D60)."""
    from misslens.trace import with_tracing

    return with_tracing({"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT})


def build_collector_graph(site: Site, fetcher: Fetcher, *, checkpointer: Any = None,
                          out_dir: Path | None = None, sleep: Callable[[float], None] | None = None,
                          clock: Callable[[], float] | None = None):
    # 테스트가 모듈을 patch할 수 있도록 기본값은 (import 시점이 아니라) 호출 시점에 정한다.
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
                # 로그아웃 상태의 코스 페이지는 리디렉션되지 않고 "Sign In"만 보이며 레슨이 없다.
                if any(site.is_expired(urljoin(url, h), None) for h in r.links):
                    return {"need_login": True}
                if any(site.is_not_enrolled(urljoin(url, h)) for h in r.links):
                    results.append({"url": url, "status": "not_enrolled", "note": "수강 등록이 필요합니다"})
                    update.update(queue=rest, results=results)
                    return update
            results.append({"url": url, "status": "expanded", "chars": len(lessons),
                            "note": f"레슨 {len(lessons)}개" + ("" if lessons else " (로그인·수강 등록 상태 확인)")})
            rest = [{"url": u, "expand": None, "title": item.get("title")} for u in lessons] + rest
        elif len(text := clean_markdown(site.strip_chrome(r.markdown))) < MIN_TEXT_CHARS:
            results.append({"url": url, "status": "empty", "chars": len(text),
                            "note": "본문이 거의 없습니다(영상만 있는 레슨이거나 추출 실패)"})
        else:
            path = save_page(site.name, url, text, out_dir)
            results.append({"url": url, "status": "saved", "path": str(path), "chars": len(text)})
        update.update(queue=rest, results=results)
        return update

    def wait_login(state: CollectState) -> CollectState:
        fetcher.close()  # 보이는 로그인 창이 프로필을 쓸 수 있게 놓아 준다. 다시 실행돼도 결과가 같다
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
