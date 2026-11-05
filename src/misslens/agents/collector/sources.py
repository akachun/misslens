"""exams/<exam_id>/sources.yaml: Collector가 읽을 수 있는 유일한 URL 목록(ARCHITECTURE §9, CLAUDE.md 규칙 7)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from misslens.exam_pack import EXAMS_DIR

MIN_DELAY_SECONDS = 2.0


class DropBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: str
    end: str


class Site(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    login_url: str
    allowed_domains: list[str] = Field(min_length=1)
    expired_markers: list[str] = Field(default_factory=list)
    not_enrolled_markers: list[str] = Field(default_factory=list)
    delay_seconds: float = Field(default=MIN_DELAY_SECONDS, ge=MIN_DELAY_SECONDS)
    drop_lines: list[str] = Field(default_factory=list)       # 정규식, 줄 전체 일치(D48)
    drop_blocks: list[DropBlock] = Field(default_factory=list)

    def strip_chrome(self, text: str) -> str:
        """추출한 텍스트에서 사이트의 페이지 장식(메뉴, 영상 플레이어 UI)을 지운다."""
        lines = [re.compile(p) for p in self.drop_lines]
        blocks = [(re.compile(b.start), re.compile(b.end)) for b in self.drop_blocks]
        src = text.splitlines()
        out: list[str] = []
        i = 0
        while i < len(src):
            line = src[i].strip()
            block = next((end for start, end in blocks if start.fullmatch(line)), None)
            if block is not None:
                j = i + 1
                while j < len(src) and not block.fullmatch(src[j].strip()):
                    j += 1
                if j == len(src):          # 끝 표시가 없으면 페이지 나머지를 버리지 않고 텍스트를 남긴다
                    out.append(src[i])
                    i += 1
                    continue
                while j + 1 < len(src) and block.fullmatch(src[j + 1].strip()):
                    j += 1
                i = j + 1
                continue
            if not any(p.fullmatch(line) for p in lines):
                out.append(src[i])
            i += 1
        return "\n".join(out)

    def allows(self, url: str) -> bool:
        u = urlparse(url)
        return u.scheme == "https" and u.hostname in self.allowed_domains

    def is_expired(self, final_url: str, status: int | None) -> bool:
        return status in (401, 403) or any(m in urlparse(final_url).path for m in self.expired_markers)

    def is_not_enrolled(self, final_url: str) -> bool:
        return any(m in urlparse(final_url).path for m in self.not_enrolled_markers)


class Source(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    site: str
    url: str
    title: str | None = None
    expand: Literal["lessons"] | None = None


class SourceList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sites: dict[str, Site]
    sources: list[Source]

    @model_validator(mode="before")
    @classmethod
    def _name_sites(cls, raw: dict) -> dict:
        sites = raw.get("sites") or {}
        return {**raw, "sites": {k: {"name": k, **v} for k, v in sites.items()}}

    @model_validator(mode="after")
    def _check(self) -> "SourceList":
        for s in self.sources:
            site = self.sites.get(s.site)
            if site is None:
                raise ValueError(f"source {s.url}: unknown site {s.site!r}")
            if not site.allows(s.url):
                raise ValueError(f"source {s.url}: not https or outside {site.allowed_domains}")
        return self

    def for_site(self, name: str) -> list[Source]:
        return [s for s in self.sources if s.site == name]


def load_sources(exam_id: str, exams_dir: Path = EXAMS_DIR) -> SourceList:
    path = Path(exams_dir) / exam_id / "sources.yaml"
    return SourceList.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
