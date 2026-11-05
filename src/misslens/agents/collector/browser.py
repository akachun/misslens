"""Collector의 Crawl4AI 쪽: 로그인 프로필과 headless 읽기 전용 수집(ARCHITECTURE §9, D12, D16).

- 로그인: `collect login <site>`가 secrets/profiles/<site>/(mode 0700)로 보이는 Chromium을 연다. 학습자가
  직접 로그인(과 코스 수강 등록)을 하고 터미널에서 q를 누른다. 에이전트는 비밀번호를 입력하거나 저장하지
  않는다(CLAUDE.md 규칙 7). 프로필에는 브라우저 자체의 세션 쿠키만 남는다.
- 수집: 같은 프로필로 headless Chromium을 쓴다. 클릭, 폼 제출, 다운로드, 스크립트 주입은 하지 않는다.

Crawl4AI는 async이고 그래프는 sync(SqliteSaver)이므로, Crawl4AIFetcher는 자기 스레드에서 이벤트 루프 하나를
돌리고 close()까지 브라우저 하나를 열어 둔다.
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
# Crawl4AI가 프로필을 만들 때와 같은 플래그. 쿠키를 OS 키체인 없이 저장하므로 headless 브라우저가
# 보이는 로그인 브라우저가 쓴 쿠키를 읽을 수 있다.
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
    """사이트 프로필로 보이는 브라우저를 열고 학습자가 q를 누를 때까지 기다린다(blocking)."""
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
        # 분석용 beacon이 있는 페이지에서는 "networkidle"이 끝나지 않는다(Skilljar에서 확인). DOM을 기다린 뒤
        # 클라이언트 렌더링에 잠깐 시간을 준다.
        run = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, wait_until="domcontentloaded",
                               page_timeout=PAGE_TIMEOUT_MS, delay_before_return_html=RENDER_WAIT_SECONDS,
                               excluded_tags=["nav", "header", "footer", "script", "style", "form", "noscript"])
        try:
            r = self._run(self._crawler.arun(url, config=run))
        except Exception as exc:  # 네트워크, 시간 초과, 브라우저 충돌: URL별로 보고하고 실행은 계속한다
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
