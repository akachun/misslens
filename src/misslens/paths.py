"""Where the learner's writable state lives, and the sandbox switch (D45).

MISSLENS_SANDBOX=1 sends the learning record and LangGraph checkpoints to data/sandbox/, so reviews, rehearsals and
demos never mix into the real statistics (accuracy, reading ratio, diagnosis agreement) and never share a SQLite
file with a running collection. The index, practice bank and collected sources are shared read-only; commands that
would write them (ingest, collect) are refused in the sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path

from misslens.bedrock import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
SANDBOX_DIR = DATA_DIR / "sandbox"


def sandbox() -> bool:
    return os.environ.get("MISSLENS_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


def state_dir() -> Path:
    return SANDBOX_DIR if sandbox() else DATA_DIR


def ledger_path() -> Path:
    return state_dir() / "ledger.sqlite"


def checkpoints_path() -> Path:
    return state_dir() / "checkpoints.sqlite"


class SandboxRefused(RuntimeError):
    """A command that writes shared material (index, collected sources) was called in the sandbox."""


def refuse_in_sandbox(what: str) -> None:
    if sandbox():
        raise SandboxRefused(f"샌드박스 모드에서는 {what}을(를) 할 수 없습니다(색인·수집본은 실제 데이터와 공유). "
                             "MISSLENS_SANDBOX를 끄고 실행하세요.")
