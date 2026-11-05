"""Build a private hand-off zip: the project plus the working data, without secrets (D56).

The public repository leaves data/ and secrets/ out (CLAUDE.md rule 7). For the instructor's hands-on review we
send, privately, a zip that runs like the current setup: the index, the source material it was built from, the
practice style bank and one learning record. Everything else is excluded on purpose.

    python scripts/make_handoff_zip.py --list                 # show what would go in, change nothing
    python scripts/make_handoff_zip.py                        # dist/misslens-handoff-<date>.zip
    python scripts/make_handoff_zip.py --record real          # ship the real record instead of the sandbox one
    python scripts/make_handoff_zip.py --record none          # no learning record (starts empty)

Safety: allow-list only (git-tracked files + the data folders below), a fixed deny-list on top, SQLite copied
through the backup API (safe while the app is running), and a final scan of every text file for credentials;
any hit stops the build.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Data folders that make the app work like the current setup (relative to the project root).
DATA_DIRS = ["data/index", "data/sources/exam_guide", "data/sources/collected", "data/sources/practice_problems",
             "data/practice"]
RECORDS = {"sandbox": "data/sandbox/ledger.sqlite", "real": "data/ledger.sqlite"}
RECORD_TARGET = "data/ledger.sqlite"

# Never shipped, even if tracked or inside an allowed folder.
DENY = [r"(^|/)\.env($|\.)", r"^secrets(/|$)", r"(^|/)\.venv(/|$)", r"(^|/)__pycache__(/|$)", r"(^|/)\.gstack(/|$)",
        r"\.egg-info(/|$)", r"(^|/)\.DS_Store$", r"checkpoints\.sqlite", r"model_status\.json$", r"^data/eval(/|$)",
        r"^data/sources/uploads(/|$)", r"\.sqlite-(wal|shm)$", r"(^|/)\.pytest_cache(/|$)", r"^dist(/|$)"]
DENY_RE = [re.compile(p) for p in DENY]

# Credential patterns checked in every text file that would be shipped.
SECRET_RE = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{30,}"), "AWS secret key"),
    (re.compile(r"(?i)(?<![a-z])(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_\-]{20,}"), "API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)aws_session_token\s*[=:]\s*\S{20,}"), "AWS session token"),
]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".jsonl", ".cfg", ".ini", ".html", ".htm",
                 ".mmd", ".csv", ".sh", ".env"}

HANDOFF = r"""# MissLens 평가용 패키지

{date} 생성 · 커밋 `{commit}` · 학습 기록: {record}

**비공개 평가용입니다.** `data/sources/`(시험 가이드, 수집한 교안, 연습 문제)와 그것으로 만든 `data/index/`는 제3자
자료이므로 평가 목적으로만 쓰고 재배포하지 마세요. 공개 저장소에는 이 데이터가 들어 있지 않습니다.

## 들어 있는 것

| 경로 | 내용 |
|---|---|
| 프로젝트 전체(코드·문서·설정·시험 팩·테스트) | 공개 저장소와 같은 내용 |
| `data/index/` | 검색 색인(bge-m3 + FAISS, {chunks}청크) |
| `data/sources/` | 색인을 만든 원본(시험 가이드, 수집 레슨, 연습 문제). 다시 색인할 때만 필요 |
| `data/practice/` | 연습 문제 문체 예시 은행 |
| `data/ledger.sqlite` | 학습 기록: {record_note} |

빠진 것: `.env`(자격 증명), `secrets/`(로그인 프로필), 그래프 체크포인트, 계정별 모델 상태. 모두 새로 만들어집니다.

## 실행

[uv](https://docs.astral.sh/uv/)와 Python 3.11 이상이 필요합니다. 압축을 푼 `misslens` 폴더에서 실행합니다.

**macOS · Linux (bash/zsh)**

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                                   # 모델 없이 도는 기본 테스트
```

**Windows (PowerShell)**

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1             # 실행 정책 오류가 나면 먼저: Set-ExecutionPolicy -Scope Process Bypass
uv pip install -e ".[dev]"
pytest
```

(명령 프롬프트 cmd.exe에서는 `.venv\Scripts\activate.bat`. PowerShell 5.1은 `&&`를 지원하지 않으니 한 줄에 하나씩 실행합니다.)

모델 호출에는 저장소 루트의 `.env`에 Amazon Bedrock 자격 증명이 필요합니다(Sonnet 4.6, Haiku 4.5 사용 승인).

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

```bash
python scripts/probe_models.py          # 쓸 수 있는 엔드포인트·모델 확인
streamlit run app/main.py               # 화면(학습·단어장·자료·현황·계획)
python -m misslens.cli retrieval-eval   # 검색 품질 점수(기준선과 비교)
```

환경변수를 붙여 실행할 때(예: 샌드박스 모드) macOS·Linux는 `MISSLENS_SANDBOX=1 streamlit run app/main.py`,
Windows PowerShell은 `$env:MISSLENS_SANDBOX="1"; streamlit run app/main.py`입니다. 교안 수집(`collect`)은 평가에 필요 없습니다.

처음 검색할 때 임베딩 모델 bge-m3(약 2GB)를 내려받습니다. 시연 순서는 `docs/demo.md`, 테스트 안내는 `docs/testing.md`에 있습니다.
"""


def denied(rel: str) -> bool:
    return any(p.search(rel) for p in DENY_RE)


def is_git_checkout() -> bool:
    return (ROOT / ".git").exists()


def tracked_files() -> list[str]:
    if not is_git_checkout():
        raise SystemExit("make_handoff_zip.py runs from the git checkout (it ships git-tracked files only); "
                         "this folder has no .git (an unpacked hand-off zip?)")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line and (ROOT / line).is_file()]


def data_files() -> list[str]:
    files = []
    for d in DATA_DIRS:
        base = ROOT / d
        if base.exists():
            files += [str(p.relative_to(ROOT)) for p in sorted(base.rglob("*")) if p.is_file()]
    return files


def plan(record: str) -> tuple[list[str], list[str], str | None]:
    """-> (included paths, denied paths that were candidates, record source path or None)."""
    candidates = sorted(set(tracked_files()) | set(data_files()))
    included = [p for p in candidates if not denied(p)]
    dropped = [p for p in candidates if denied(p)]
    src = RECORDS.get(record)
    if src and not (ROOT / src).exists():
        raise SystemExit(f"learning record {src} does not exist (use --record none)")
    return included, dropped, src


def scan_secrets(paths: list[Path], root: Path) -> list[str]:
    hits = []
    for p in paths:
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"Makefile", "Dockerfile"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, what in SECRET_RE:
            if rx.search(text):
                hits.append(f"{p.relative_to(root)}: {what}")
    return hits


def copy_sqlite(src: Path, dst: Path) -> None:
    """Consistent snapshot even while the app writes (WAL); the copy has no -wal/-shm companions."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    with sqlite3.connect(dst) as d:
        d.execute("PRAGMA journal_mode=DELETE")


def build(record: str, out: Path) -> Path:
    included, _, rec_src = plan(record)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        rec_path = None
        if rec_src:
            rec_path = stage / RECORD_TARGET
            copy_sqlite(ROOT / rec_src, rec_path)
        chunks = sum(1 for _ in open(ROOT / "data/index/ccar-f/chunks.jsonl", encoding="utf-8")) \
            if (ROOT / "data/index/ccar-f/chunks.jsonl").exists() else 0
        note = {"sandbox": "샌드박스에서 리허설한 세트·진단·단어장·계획(데모용)", "real": "실제 학습 기록",
                None: "없음(빈 상태에서 시작)"}[rec_src and record]
        handoff = stage / "HANDOFF.md"
        handoff.write_text(HANDOFF.format(date=datetime.now().strftime("%Y-%m-%d %H:%M"), commit=commit + (" (+로컬 변경)" if dirty else ""),
                                          record=record, chunks=chunks, record_note=note), encoding="utf-8")
        hits = scan_secrets([ROOT / p for p in included] + [handoff], ROOT)
        if hits:
            raise SystemExit("credential-like text found, nothing written:\n  " + "\n  ".join(hits))
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            top = "misslens"
            for p in included:
                z.write(ROOT / p, f"{top}/{p}")
            if rec_path is not None:
                z.write(rec_path, f"{top}/{RECORD_TARGET}")
            z.write(handoff, f"{top}/HANDOFF.md")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", choices=["sandbox", "real", "none"], default="sandbox",
                    help="learning record to ship as data/ledger.sqlite (default: the sandbox rehearsal record)")
    ap.add_argument("--list", action="store_true", help="print what would be included and excluded, write nothing")
    ap.add_argument("--out", type=Path, help="zip path (default dist/misslens-handoff-<date>.zip)")
    args = ap.parse_args(argv)
    included, dropped, rec_src = plan(args.record)
    if args.list:
        size = sum((ROOT / p).stat().st_size for p in included)
        print(f"include {len(included)} files ({size / 1e6:.1f} MB)" + (f" + record {rec_src} -> {RECORD_TARGET}" if rec_src else ""))
        for d in ["data/"] + [p for p in ("app", "config", "docs", "evals", "exams", "scripts", "src", "tests")]:
            n = sum(1 for p in included if p.startswith(d))
            print(f"  {d:12} {n} files")
        print(f"excluded by rule: {len(dropped)}" + "".join(f"\n  - {p}" for p in dropped[:20]))
        hits = scan_secrets([ROOT / p for p in included], ROOT)
        print("credential scan: " + ("OK" if not hits else "FOUND\n  " + "\n  ".join(hits)))
        return 1 if hits else 0
    out = args.out or ROOT / "dist" / f"misslens-handoff-{datetime.now():%Y%m%d-%H%M}.zip"
    build(args.record, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
