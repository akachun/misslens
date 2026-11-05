"""Crawl4AI side of the Collector: login profile and headless read-only fetching (ARCHITECTURE §9, D12, D16).

- Login: `collect login <site>` opens a visible Chromium on secrets/profiles/<site>/ (mode 0700). The learner
  signs in (and enrolls in courses) by hand, then presses q in the terminal. The agent never types or stores
  a password (CLAUDE.md rule 7); the profile keeps only the browser's own session cookies.
- Fetch: headless Chromium on the same profile. No clicks, no form submits, no downloads, no scripts injected.

Crawl4AI is async; the graph is sync (SqliteSaver), so Crawl4AIFetcher runs one event loop in its own thread
and keeps one browser open until close().
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from misslens.bedrock import PROJECT_ROOT

PROFILES_DIR = PROJECT_ROOT / "secrets" / "profiles"
# Same flags as Crawl4AI's profile creation: cookies are stored without the OS keychain, so the headless
# browser can read what the visible login browser wrote.
PORTABLE_PROFILE_ARGS = ["--password-store=basic", "--use-mock-keychain"]
PAGE_TIMEOUT_MS = 60_000
RENDER_WAIT_SECONDS = 2.0


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    markdown: str = ""
    links: list[str] = field(default_factory=list)
    error: str | None = None


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...

    def close(self) -> None: ...


def profile_dir(site: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in site)
    return PROFILES_DIR / safe


def _secure_dir(path: Path) -> None:
    for p in (PROFILES_DIR.parent, PROFILES_DIR, path):
        p.mkdir(exist_ok=True)
        os.chmod(p, 0o700)


def login(site: str, login_url: str) -> Path:
    """Open a visible browser on the site's profile and wait until the learner presses q (blocking)."""
    from crawl4ai import BrowserConfig, BrowserProfiler

    path = profile_dir(site)
    _secure_dir(path)
    profiler = BrowserProfiler()
    profiler.profiles_dir = str(PROFILES_DIR)
    print(f"\n브라우저 창이 열리면 주소창에 아래 주소를 열어 직접 로그인하세요(비밀번호는 저장하지 않습니다).\n"
          f"  {login_url}\n수강 등록이 필요한 코스는 창에서 직접 등록하세요. 끝나면 이 터미널에서 q를 누릅니다.\n")
    asyncio.run(profiler.create_profile(profile_name=path.name,
                                        browser_config=BrowserConfig(browser_type="chromium", headless=False)))
    os.chmod(path, 0o700)
    return path


class Crawl4AIFetcher:
    def __init__(self, site: str):
        self.profile = profile_dir(site)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._crawler = None

    def _ensure(self) -> None:
        if self._crawler is not None:
            return
        if not self.profile.exists():
            raise RuntimeError(f"로그인 프로필이 없습니다: 먼저 `collect login`을 실행하세요 ({self.profile.name})")
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="collector-browser", daemon=True)
        self._thread.start()
        cfg = BrowserConfig(browser_type="chromium", headless=True, use_managed_browser=True,
                            user_data_dir=str(self.profile), extra_args=list(PORTABLE_PROFILE_ARGS), verbose=False)
        crawler = AsyncWebCrawler(config=cfg)
        self._run(crawler.start())
        self._crawler = crawler

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=PAGE_TIMEOUT_MS / 1000 + 60)

    def fetch(self, url: str) -> FetchResult:
        from crawl4ai import CacheMode, CrawlerRunConfig

        self._ensure()
        # "networkidle" never settles on pages with analytics beacons (seen on Skilljar): wait for the DOM, then
        # give client-side rendering a moment.
        run = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, wait_until="domcontentloaded",
                               page_timeout=PAGE_TIMEOUT_MS, delay_before_return_html=RENDER_WAIT_SECONDS,
                               excluded_tags=["nav", "header", "footer", "script", "style", "form", "noscript"])
        try:
            r = self._run(self._crawler.arun(url, config=run))
        except Exception as exc:  # network, timeout, browser crash: reported per URL, the run goes on
            return FetchResult(url, url, None, error=f"{type(exc).__name__}: {exc}"[:300])
        final = r.redirected_url or r.url or url
        md = r.markdown
        text = getattr(md, "raw_markdown", None) or (md if isinstance(md, str) else "") or ""
        links = [l.get("href", "") for l in (r.links or {}).get("internal", []) if l.get("href")]
        return FetchResult(url, final, r.status_code, text, links,
                           None if r.success else (r.error_message or "crawl failed")[:300])

    def close(self) -> None:
        if self._crawler is not None:
            try:
                self._run(self._crawler.close())
            finally:
                self._crawler = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)
            self._loop.close()
            self._loop = None
            self._thread = None
