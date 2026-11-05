"""D45: 샌드박스 모드는 검토와 리허설을 실제 기록과 공유 자료에서 떼어 놓는다."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import misslens.paths as paths
from misslens.cli import main
from misslens.ledger import Ledger


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SANDBOX_DIR", tmp_path / "sandbox")
    monkeypatch.setenv("MISSLENS_SANDBOX", "1")
    return tmp_path / "sandbox"


def test_paths_switch_with_env(sandbox, monkeypatch):
    assert paths.sandbox() and paths.ledger_path() == sandbox / "ledger.sqlite"
    assert paths.checkpoints_path() == sandbox / "checkpoints.sqlite"
    monkeypatch.setenv("MISSLENS_SANDBOX", "0")
    assert paths.ledger_path() == paths.DATA_DIR / "ledger.sqlite"


def test_ledger_default_goes_to_sandbox(sandbox):
    lg = Ledger()
    try:
        assert lg.path == str(sandbox / "ledger.sqlite") and (sandbox / "ledger.sqlite").exists()
    finally:
        lg.close()


def test_checkpointer_default_goes_to_sandbox(sandbox):
    from misslens.agents.collector import sqlite_checkpointer

    sqlite_checkpointer()
    assert (sandbox / "checkpoints.sqlite").exists()


@pytest.mark.parametrize("argv", [["ingest", "README.md"], ["collect", "run"], ["practice", "import"]])
def test_cli_refuses_shared_writes(sandbox, argv, capsys):
    assert main(argv) == 2
    assert not (sandbox / "ledger.sqlite").exists()             # 아무것도 열기 전에 거부


@pytest.mark.parametrize("intent", ["collect", "ingest"])
def test_supervisor_refuses_shared_writes(sandbox, intent):
    from fakes import FakeRoleLLM  # noqa: F401  (fakes를 함께 import할 수 있게 유지)
    from misslens.exam_pack import load_exam_pack
    from misslens.graph import Services, build_supervisor_graph

    lg = Ledger(":memory:")
    svc = Services(pack=load_exam_pack("ccar-f"), ledger=lg, make_generator=lambda: None)
    out = build_supervisor_graph(svc, checkpointer=InMemorySaver()).invoke(
        {"intent": intent, "args": {}}, {"configurable": {"thread_id": "t"}})
    lg.close()
    assert "샌드박스" in out["message"]
