"""업로드한 파일과 붙여 넣은 텍스트를 일반 텍스트 문서로 바꾼다(SERVICE F1).

지원 형식: .pdf(pypdf, 페이지 단위), .md / .markdown / .txt, .html / .htm(BeautifulSoup 텍스트).
웹 fetch 도구로 저장한 파일은 "BEGIN UNTRUSTED EXTERNAL CONTENT (source: URL)" 표시로 시작할 수 있다.
표시 줄은 지우고 URL은 문서의 source로 쓴다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".html", ".htm"}
_MARKER = re.compile(r"^-{2,}\s*(BEGIN|END) UNTRUSTED EXTERNAL CONTENT(?:\s*\(source:\s*(?P<url>[^)]+)\))?\s*-{2,}\s*$",
                     re.MULTILINE)


class UnsupportedFile(ValueError):
    pass


@dataclass(frozen=True)
class Document:
    text: str
    source: str        # 프롬프트에 보이고 materials.source_uri로 저장된다
    source_type: str   # 'upload' | 'paste' | 'collect' (materials.source_type)

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


def content_hash(text: str) -> str:
    """공백을 정규화한 텍스트의 해시. 파일을 다시 저장해도 중복이 생기지 않는다."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _strip_markers(text: str) -> tuple[str, str | None]:
    url = None
    for m in _MARKER.finditer(text):
        url = url or (m.group("url") or "").strip() or None
    return _MARKER.sub("", text).strip(), url


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    pages = []
    for i, page in enumerate(PdfReader(str(path)).pages, start=1):
        t = (page.extract_text() or "").strip()
        if t:
            pages.append(f"[page {i}]\n{t}")
    return "\n\n".join(pages)


def _html_text(raw: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def load_file(path: Path | str) -> Document:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFile(f"{path.name}: 지원하지 않는 형식입니다({', '.join(sorted(SUPPORTED))}만 가능)")
    if ext == ".pdf":
        text, url = _pdf_text(path), None
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw, url = _strip_markers(raw)
        text = _html_text(raw) if ext in {".html", ".htm"} else raw
    return Document(text=text.strip(), source=url or path.name, source_type="upload")


def from_paste(text: str, label: str = "paste") -> Document:
    return Document(text=text.strip(), source=label, source_type="paste")
