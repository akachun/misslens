"""D56: 비공개 전달용 zip에는 자격 증명이나 로컬 전용 상태가 절대 들어가지 않는다."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("make_handoff_zip", ROOT / "scripts" / "make_handoff_zip.py")
mh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mh)


def test_deny_list_blocks_secrets_and_local_state():
    for p in [".env", ".env.local", "secrets/profiles/skilljar/Default/Cookies", "data/checkpoints.sqlite",
              "data/sandbox/checkpoints.sqlite", "data/model_status.json", "data/ledger.sqlite-wal",
              "data/eval/misses.yaml", "data/sources/uploads/x.pdf", ".venv/bin/python", "src/misslens/__pycache__/a.pyc",
              ".gstack/browse.log", "src/misslens.egg-info/PKG-INFO", "dist/old.zip", "docs/.DS_Store"]:
        assert mh.denied(p), p
    for p in ["src/misslens/cli.py", "data/index/ccar-f/faiss.index", "data/sources/collected/skilljar/a.md",
              "data/practice/ccar-f/bank.jsonl", ".streamlit/config.toml", "docs/testing.md", "README.md"]:
        assert not mh.denied(p), p


def test_secret_scan_finds_planted_credentials(tmp_path):
    clean = tmp_path / "ok.md"
    clean.write_text("AWS_ACCESS_KEY_ID=\nsee README for credentials\n", encoding="utf-8")
    leaked = tmp_path / "leak.yaml"
    leaked.write_text("aws_secret_access_key: " + "a" * 40 + "\nkey: AKIA" + "B" * 16 + "\n", encoding="utf-8")
    hits = mh.scan_secrets([clean, leaked], tmp_path)
    assert hits and all(h.startswith("leak.yaml") for h in hits) and len(hits) == 2


def test_sqlite_copy_is_a_consistent_single_file(tmp_path):
    src = tmp_path / "live.sqlite"
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()                                             # 아직 WAL에 있고, 연결이 열려 있다
    dst = tmp_path / "out" / "copy.sqlite"
    mh.copy_sqlite(src, dst)
    con.close()
    with sqlite3.connect(dst) as d:
        assert d.execute("SELECT x FROM t").fetchall() == [(1,)]
        assert d.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not (tmp_path / "out" / "copy.sqlite-wal").exists()


import pytest


@pytest.mark.skipif(not mh.is_git_checkout(), reason="needs the git checkout (an unpacked hand-off zip has no .git)")
def test_plan_never_includes_denied_paths():
    included, _, _ = mh.plan("none")
    assert included and not any(mh.denied(p) for p in included)
    assert any(p.startswith("src/misslens/") for p in included)
