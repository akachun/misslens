"""exams/<exam_id>/sources.yaml: the only URLs the Collector may read (ARCHITECTURE §9, CLAUDE.md rule 7)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from misslens.exam_pack import EXAMS_DIR

MIN_DELAY_SECONDS = 2.0


class Site(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    login_url: str
    allowed_domains: list[str] = Field(min_length=1)
    expired_markers: list[str] = Field(default_factory=list)
    not_enrolled_markers: list[str] = Field(default_factory=list)
    delay_seconds: float = Field(default=MIN_DELAY_SECONDS, ge=MIN_DELAY_SECONDS)

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
