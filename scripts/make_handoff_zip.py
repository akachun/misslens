"""비공개 전달용 zip을 만든다: 프로젝트와 작업 데이터를 담고, 비밀 정보는 뺀다(D56).

공개 저장소에는 data/와 secrets/가 빠져 있다(CLAUDE.md 규칙 7). 강사의 실습 검토를 위해, 지금 환경처럼 돌아가는
zip을 비공개로 보낸다: 색인, 색인을 만든 원본 자료, 연습 문제 문체 은행, 학습 기록 하나. 나머지는 모두 일부러 뺀다.

    python scripts/make_handoff_zip.py --list                 # 무엇이 들어갈지 보여 주고, 아무것도 바꾸지 않는다
    python scripts/make_handoff_zip.py                        # dist/misslens-handoff-<date>.zip
    python scripts/make_handoff_zip.py --record real          # 샌드박스 기록 대신 실제 기록을 넣는다
    python scripts/make_handoff_zip.py --record none          # 학습 기록 없음(빈 상태로 시작)

안전장치: 허용 목록만 쓴다(git이 추적하는 파일 + 아래 데이터 폴더). 그 위에 고정 거부 목록을 적용하고, SQLite는
backup API로 복사하며(앱이 실행 중이어도 안전), 마지막으로 모든 텍스트 파일에서 자격 증명을 검사한다.
하나라도 걸리면 빌드를 멈춘다.
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

# 앱이 지금 환경처럼 돌아가게 하는 데이터 폴더(프로젝트 루트 기준 상대 경로).
DATA_DIRS = ["data/index", "data/sources/exam_guide", "data/sources/collected", "data/sources/practice_problems",
             "data/practice"]
RECORDS = {"sandbox": "data/sandbox/ledger.sqlite", "real": "data/ledger.sqlite"}
RECORD_TARGET = "data/ledger.sqlite"

# git이 추적하거나 허용된 폴더 안에 있더라도 절대 넣지 않는다.
DENY = [r"(^|/)\.env($|\.)", r"^secrets(/|$)", r"(^|/)\.venv(/|$)", r"(^|/)__pycache__(/|$)", r"(^|/)\.gstack(/|$)",
        r"\.egg-info(/|$)", r"(^|/)\.DS_Store$", r"checkpoints\.sqlite", r"model_status\.json$", r"^data/eval(/|$)",
        r"^data/sources/uploads(/|$)", r"\.sqlite-(wal|shm)$", r"(^|/)\.pytest_cache(/|$)", r"^dist(/|$)"]
DENY_RE = [re.compile(p) for p in DENY]

# 내보낼 모든 텍스트 파일에서 검사하는 자격 증명 패턴.
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

Python 3.11 이상이 필요합니다. 압축을 푼 `misslens` 폴더에서 실행합니다. 아래는 [uv](https://docs.astral.sh/uv/) 기준이고,
uv가 없으면 맨 아래 'uv 없이'를 따릅니다. 의존성(torch, sentence-transformers 등) 설치에 몇 분 걸립니다.

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

**uv 없이 (Python 기본 venv + pip)**

```
python -m venv .venv
.venv\Scripts\activate            (macOS·Linux: source .venv/bin/activate)
python -m pip install -U pip
pip install -e ".[dev]"
pytest                             기대 결과: 308 passed, 1 skipped
```

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

`streamlit run` 뒤 터미널에 나오는 주소(기본 http://localhost:8501)를 브라우저에서 엽니다(자동으로 열리지 않음,
Streamlit의 첫 실행 이메일 질문도 나오지 않게 설정돼 있음. 다른 설정으로 질문이 나오면 Enter로 건너뜀).
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
    """-> (포함할 경로, 후보였지만 거부된 경로, 학습 기록 원본 경로 또는 None)."""
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
    """앱이 쓰는 중(WAL)에도 일관된 스냅샷을 만든다. 복사본에는 -wal/-shm 파일이 딸려 오지 않는다."""
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
