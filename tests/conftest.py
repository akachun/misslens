import sys
from pathlib import Path

# tests/fakes.py를 `fakes`로 import할 수 있게 한다.
sys.path.insert(0, str(Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _no_writes_to_real_collection(tmp_path, monkeypatch):
    """모든 테스트는 수집한 페이지를 data/sources/collected/가 아니라 tmp_path 아래에 저장한다(2026-11-05 발견)."""
    import misslens.agents.collector.graph as cg

    monkeypatch.setattr(cg, "COLLECTED_DIR", tmp_path / "collected-default")


@pytest.fixture(autouse=True)
def _no_writes_to_real_traces(tmp_path, monkeypatch):
    """트레이스(D60)도 data/traces/가 아니라 tmp_path 아래에 쓴다(첫 실행에서 테스트 기록이 실제 폴더에 남았다)."""
    import misslens.trace as tr

    monkeypatch.setattr(tr, "trace_dir", lambda: tmp_path / "traces")
