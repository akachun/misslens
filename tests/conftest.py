import sys
from pathlib import Path

# Make tests/fakes.py importable as `fakes`.
sys.path.insert(0, str(Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _no_writes_to_real_collection(tmp_path, monkeypatch):
    """Every test saves collected pages under tmp_path, never into data/sources/collected/ (found 2026-11-05)."""
    import misslens.agents.collector.graph as cg

    monkeypatch.setattr(cg, "COLLECTED_DIR", tmp_path / "collected-default")
