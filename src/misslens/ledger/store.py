"""Ledger storage: the only module that reads or writes ledger.sqlite (ARCHITECTURE §5, D9, D10).

Writes that must be atomic (set submission, review) run in one transaction and are guarded by
quiz_sets.status, so a double submit fails instead of writing twice.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from misslens.bedrock import PROJECT_ROOT
from misslens.ledger.judge import judge, missed_phrase

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "ledger.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quiz_sets (
    id            INTEGER PRIMARY KEY,
    exam_id       TEXT NOT NULL,
    size          INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('generating','in_progress','submitted','reviewed')),
    created_at    TEXT NOT NULL,
    submitted_at  TEXT,
    reviewed_at   TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id                INTEGER PRIMARY KEY,
    set_id            INTEGER NOT NULL REFERENCES quiz_sets(id),
    exam_id           TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    domain            TEXT NOT NULL,
    blueprint_version INTEGER NOT NULL,
    seq               INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ready','failed')),
    payload_json      TEXT,
    UNIQUE (set_id, seq),
    CHECK (status = 'failed' OR payload_json IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY,
    set_id           INTEGER NOT NULL REFERENCES quiz_sets(id),
    question_id      INTEGER NOT NULL UNIQUE REFERENCES questions(id),
    chosen_index     INTEGER,
    is_correct       INTEGER NOT NULL CHECK (is_correct IN (0,1)),
    elapsed_ms       INTEGER,
    is_decoy         INTEGER NOT NULL DEFAULT 0 CHECK (is_decoy IN (0,1)),
    reask_choice     INTEGER,
    reask_confident  INTEGER CHECK (reask_confident IN (0,1)),
    miss_type        TEXT CHECK (miss_type IN ('concept','reading','uncertain')),
    invalid          INTEGER NOT NULL DEFAULT 0 CHECK (invalid IN (0,1))
);
CREATE TABLE IF NOT EXISTS missed_phrases (
    attempt_id      INTEGER PRIMARY KEY REFERENCES attempts(id),
    phrase          TEXT NOT NULL,
    user_corrected  INTEGER NOT NULL DEFAULT 0 CHECK (user_corrected IN (0,1))
);
CREATE TABLE IF NOT EXISTS materials (
    id            INTEGER PRIMARY KEY,
    exam_id       TEXT NOT NULL,
    source_type   TEXT NOT NULL CHECK (source_type IN ('upload','paste','collect')),
    source_uri    TEXT,
    content_hash  TEXT NOT NULL,
    task_id       TEXT,
    collected_at  TEXT NOT NULL,
    UNIQUE (exam_id, content_hash)
);
CREATE TABLE IF NOT EXISTS generation_failures (
    id          INTEGER PRIMARY KEY,
    exam_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    tries       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_question ON attempts(question_id);
CREATE INDEX IF NOT EXISTS ix_attempts_set ON attempts(set_id);
CREATE INDEX IF NOT EXISTS ix_questions_exam_task ON questions(exam_id, task_id);
"""


class LedgerError(RuntimeError):
    pass


class SetStateError(LedgerError):
    """The set is not in a state that allows this operation (e.g. double submit)."""


@dataclass(frozen=True)
class Answer:
    question_id: int
    chosen_index: int | None  # None = left unanswered (counts as wrong)
    elapsed_ms: int | None


@dataclass(frozen=True)
class ReaskAnswer:
    """One row of the re-ask form, with reask_choice mapped back to the ORIGINAL option order."""
    attempt_id: int
    is_decoy: bool
    reask_choice: int | None
    reask_confident: bool | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across threads (Streamlit, generator thread), serialized by a lock.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        """Read helper for the ledger package (stats). Private: rule 1 keeps SQL inside misslens.ledger."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- sets and questions ---------------------------------------------------------------

    def create_set(self, exam_id: str, size: int) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO quiz_sets (exam_id, size, status, created_at) VALUES (?,?, 'generating', ?)",
                            (exam_id, size, _now()))
            return int(cur.lastrowid)

    def start_set(self, set_id: int) -> None:
        """First question shown: generating -> in_progress (no-op if already in progress)."""
        with self._tx() as c:
            status = self._set_status(c, set_id)
            if status == "generating":
                c.execute("UPDATE quiz_sets SET status='in_progress' WHERE id=?", (set_id,))
            elif status != "in_progress":
                raise SetStateError(f"set {set_id} is {status}; cannot start")

    def add_question(self, set_id: int, *, seq: int, task_id: str, domain: str, blueprint_version: int,
                     payload: dict[str, Any] | None, status: str = "ready") -> int:
        """Store a question that PASSED validation (status ready), or a failed slot (§4.1).

        Rule 2: never call this with a question that failed the validator or grounding check.
        """
        if status == "ready" and payload is None:
            raise ValueError("ready question needs a payload")
        with self._tx() as c:
            row = c.execute("SELECT exam_id, status FROM quiz_sets WHERE id=?", (set_id,)).fetchone()
            if row is None:
                raise LedgerError(f"no set {set_id}")
            if row["status"] not in ("generating", "in_progress"):
                raise SetStateError(f"set {set_id} is {row['status']}; cannot add questions")
            cur = c.execute(
                "INSERT INTO questions (set_id, exam_id, task_id, domain, blueprint_version, seq, status, payload_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (set_id, row["exam_id"], task_id, domain, blueprint_version, seq, status,
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None))
            return int(cur.lastrowid)

    def submit_set(self, set_id: int, answers: list[Answer]) -> list[int]:
        """Grade and store all answers in one transaction; returns attempt ids in input order.

        `answers` must cover every ready question of the set; pass chosen_index=None for one left
        unanswered so it counts as wrong instead of silently disappearing from the statistics.
        """
        with self._tx() as c:
            cur = c.execute("UPDATE quiz_sets SET status='submitted', submitted_at=? "
                            "WHERE id=? AND status IN ('generating','in_progress')", (_now(), set_id))
            if cur.rowcount != 1:
                raise SetStateError(f"set {set_id} is {self._set_status(c, set_id)}; cannot submit")
            ready = {r["id"]: json.loads(r["payload_json"]) for r in c.execute(
                "SELECT id, payload_json FROM questions WHERE set_id=? AND status='ready'", (set_id,))}
            seen: set[int] = set()
            ids: list[int] = []
            for a in answers:
                if a.question_id not in ready:
                    raise LedgerError(f"question {a.question_id} is not a ready question of set {set_id}")
                if a.question_id in seen:
                    raise LedgerError(f"question {a.question_id} answered twice")
                seen.add(a.question_id)
                payload = ready[a.question_id]
                n_opts = len(payload["options"])
                if a.chosen_index is not None and not 0 <= a.chosen_index < n_opts:
                    raise LedgerError(f"chosen_index {a.chosen_index} out of range")
                is_correct = int(a.chosen_index is not None and a.chosen_index == payload["answer_index"])
                cur = c.execute("INSERT INTO attempts (set_id, question_id, chosen_index, is_correct, elapsed_ms)"
                                " VALUES (?,?,?,?,?)", (set_id, a.question_id, a.chosen_index, is_correct, a.elapsed_ms))
                ids.append(int(cur.lastrowid))
            unanswered = sorted(set(ready) - seen)
            if unanswered:
                raise LedgerError(f"set {set_id}: ready questions {unanswered} have no answer "
                                  "(pass chosen_index=None for unanswered)")
            return ids

    def record_review(self, set_id: int, reasks: list[ReaskAnswer], *, reask_skipped: bool = False) -> None:
        """Apply the §4.2 table and store the review in one transaction.

        Every valid miss of the set must be included. Decoys must be attempts answered correctly.
        reask_skipped=True (exam and learner language are the same): pass misses with
        reask_choice=None; they are all stored as concept, and no decoys are allowed.
        """
        with self._tx() as c:
            cur = c.execute("UPDATE quiz_sets SET status='reviewed', reviewed_at=? WHERE id=? AND status='submitted'",
                            (_now(), set_id))
            if cur.rowcount != 1:
                raise SetStateError(f"set {set_id} is {self._set_status(c, set_id)}; cannot review")
            rows = {r["id"]: r for r in c.execute(
                "SELECT a.id, a.chosen_index, a.is_correct, a.invalid, q.payload_json FROM attempts a "
                "JOIN questions q ON q.id = a.question_id WHERE a.set_id=?", (set_id,))}
            # Misses already flagged '문제 이상' are not re-asked (they are out of every statistic).
            misses = {i for i, r in rows.items() if not r["is_correct"] and not r["invalid"]}
            given = [r.attempt_id for r in reasks]
            if len(given) != len(set(given)):
                raise LedgerError("attempt listed twice in review")
            unknown = set(given) - set(rows)
            if unknown:
                raise LedgerError(f"attempts {sorted(unknown)} are not in set {set_id}")
            missing = misses - set(given)
            if missing:
                raise LedgerError(f"review must include every miss; missing {sorted(missing)}")
            for r in reasks:
                row = rows[r.attempt_id]
                first_correct = bool(row["is_correct"])
                if r.is_decoy and not first_correct:
                    raise LedgerError(f"attempt {r.attempt_id}: a decoy must be a correctly answered question")
                if not r.is_decoy and first_correct:
                    raise LedgerError(f"attempt {r.attempt_id}: correct answers can only be re-asked as decoys")
                if reask_skipped:
                    if r.is_decoy or r.reask_choice is not None:
                        raise LedgerError("reask_skipped review cannot carry decoys or re-ask answers")
                elif r.reask_choice is None or r.reask_confident is None:
                    raise LedgerError(f"attempt {r.attempt_id}: re-ask answer and confidence are required")
                payload = json.loads(row["payload_json"])
                if r.reask_choice is not None and not 0 <= r.reask_choice < len(payload["options"]):
                    raise LedgerError(f"attempt {r.attempt_id}: reask_choice {r.reask_choice} out of range")
                reask_correct = None if r.reask_choice is None else r.reask_choice == payload["answer_index"]
                mt = judge(first_correct=first_correct, is_decoy=r.is_decoy,
                           reask_correct=reask_correct, reask_confident=r.reask_confident)
                c.execute("UPDATE attempts SET is_decoy=?, reask_choice=?, reask_confident=?, miss_type=? WHERE id=?",
                          (int(r.is_decoy), r.reask_choice,
                           None if r.reask_confident is None else int(r.reask_confident), mt, r.attempt_id))
                phrase = missed_phrase(mt, payload, row["chosen_index"])
                if phrase:
                    c.execute("INSERT INTO missed_phrases (attempt_id, phrase) VALUES (?,?)", (r.attempt_id, phrase))

    def correct_missed_phrase(self, attempt_id: int, phrase: str) -> None:
        """Learner edits the recorded phrase. Only reading misses carry a phrase (§4.2, rule 3)."""
        with self._tx() as c:
            row = c.execute("SELECT miss_type FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise LedgerError(f"no attempt {attempt_id}")
            if row["miss_type"] != "reading":
                raise LedgerError(f"attempt {attempt_id} is {row['miss_type']!r}; only reading misses have a phrase")
            c.execute("INSERT INTO missed_phrases (attempt_id, phrase, user_corrected) VALUES (?,?,1) "
                      "ON CONFLICT(attempt_id) DO UPDATE SET phrase=excluded.phrase, user_corrected=1",
                      (attempt_id, phrase))

    def mark_invalid(self, attempt_id: int, invalid: bool = True) -> None:
        """'문제 이상' (F8): the attempt drops out of every statistic, including phrase ranking."""
        with self._tx() as c:
            if c.execute("UPDATE attempts SET invalid=? WHERE id=?", (int(invalid), attempt_id)).rowcount != 1:
                raise LedgerError(f"no attempt {attempt_id}")

    # --- materials and failures -----------------------------------------------------------

    def add_material(self, exam_id: str, *, source_type: str, content_hash: str,
                     source_uri: str | None = None, task_id: str | None = None) -> int | None:
        """Returns the new id, or None if the same content is already stored for this exam."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO materials (exam_id, source_type, source_uri, content_hash, task_id,"
                            " collected_at) VALUES (?,?,?,?,?,?)",
                            (exam_id, source_type, source_uri, content_hash, task_id, _now()))
            return int(cur.lastrowid) if cur.rowcount == 1 else None

    def record_generation_failure(self, exam_id: str, task_id: str, tries: int, reason: str) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO generation_failures (exam_id, task_id, tries, reason, created_at)"
                            " VALUES (?,?,?,?,?)", (exam_id, task_id, tries, reason, _now()))
            return int(cur.lastrowid)

    # --- reads for the core loop ----------------------------------------------------------

    def get_set(self, set_id: int) -> dict[str, Any]:
        rows = self._query("SELECT * FROM quiz_sets WHERE id=?", (set_id,))
        if not rows:
            raise LedgerError(f"no set {set_id}")
        return dict(rows[0])

    def set_questions(self, set_id: int) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM questions WHERE set_id=? ORDER BY seq", (set_id,)):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json")) if d["payload_json"] else None
            out.append(d)
        return out

    def set_attempts(self, set_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT a.*, m.phrase AS missed_phrase FROM attempts a "
            "LEFT JOIN missed_phrases m ON m.attempt_id = a.id WHERE a.set_id=? ORDER BY a.id", (set_id,))]

    @staticmethod
    def _set_status(c: sqlite3.Connection, set_id: int) -> str:
        row = c.execute("SELECT status FROM quiz_sets WHERE id=?", (set_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no set {set_id}")
        return row["status"]
