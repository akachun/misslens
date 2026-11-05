"""학습자의 쓰기 가능한 상태가 저장되는 위치와 샌드박스 스위치(D45).

MISSLENS_SANDBOX=1이면 학습 기록과 LangGraph checkpoint를 data/sandbox/에 둔다. 그래서 검토, 리허설, 시연이
실제 통계(정답률, 해석 실수 비율, 진단 일치율)에 섞이지 않고, 실행 중인 수집과 SQLite 파일을 공유하지도 않는다.
색인, 연습 문제 은행, 수집한 자료는 읽기 전용으로 공유한다. 이들을 쓰게 될 명령(ingest, collect)은
샌드박스에서 거부한다.
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
    """공유 자료(색인, 수집한 자료)를 쓰는 명령이 샌드박스에서 호출되었다."""


def refuse_in_sandbox(what: str) -> None:
    if sandbox():
        raise SandboxRefused(f"샌드박스 모드에서는 {what}을(를) 할 수 없습니다(색인·수집본은 실제 데이터와 공유). "
                             "MISSLENS_SANDBOX를 끄고 실행하세요.")
