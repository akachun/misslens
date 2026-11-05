"""Ledger 저장소: ledger.sqlite를 읽고 쓰는 유일한 모듈(ARCHITECTURE §5, D9, D10).

원자적이어야 하는 쓰기(세트 제출, 리뷰)는 한 트랜잭션에서 실행하고 quiz_sets.status로 막는다.
그래서 두 번 제출하면 두 번 기록되지 않고 실패한다.
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

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "ledger.sqlite"   # 실제 기록. 샌드박스는 misslens.paths.ledger_path()가 반영한다

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
CREATE TABLE IF NOT EXISTS miss_feedback (
    attempt_id     INTEGER PRIMARY KEY REFERENCES attempts(id),
    learner_label  TEXT NOT NULL CHECK (learner_label IN ('concept','reading','unsure')),
    stage          TEXT NOT NULL DEFAULT 'before_explanation',
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY,
    exam_id       TEXT NOT NULL,
    set_id        INTEGER REFERENCES quiz_sets(id),
    kind          TEXT NOT NULL CHECK (kind IN ('plan','quick')),
    total         INTEGER NOT NULL,
    counts_json   TEXT,
    rationale     TEXT,
    source        TEXT,
    thread_id     TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_answers (
    question_id   INTEGER PRIMARY KEY REFERENCES questions(id),
    set_id        INTEGER NOT NULL REFERENCES quiz_sets(id),
    chosen_index  INTEGER,
    elapsed_ms    INTEGER,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab (
    id            INTEGER PRIMARY KEY,
    exam_id       TEXT NOT NULL,
    set_id        INTEGER NOT NULL REFERENCES quiz_sets(id),
    question_id   INTEGER NOT NULL REFERENCES questions(id),
    term          TEXT NOT NULL,
    term_key      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('condition','technical','general')),
    meaning       TEXT NOT NULL,
    role          TEXT,
    from_miss     INTEGER NOT NULL DEFAULT 0 CHECK (from_miss IN (0,1)),
    created_at    TEXT NOT NULL,
    UNIQUE (set_id, term_key)
);
CREATE TABLE IF NOT EXISTS vocab_known (
    exam_id   TEXT NOT NULL,
    term_key  TEXT NOT NULL,
    known_at  TEXT NOT NULL,
    PRIMARY KEY (exam_id, term_key)
);
CREATE INDEX IF NOT EXISTS ix_attempts_question ON attempts(question_id);
CREATE INDEX IF NOT EXISTS ix_attempts_set ON attempts(set_id);
CREATE INDEX IF NOT EXISTS ix_questions_exam_task ON questions(exam_id, task_id);
"""


class LedgerError(RuntimeError):
    pass


class SetStateError(LedgerError):
    """세트가 이 작업을 허용하지 않는 상태다(예: 두 번 제출)."""


@dataclass(frozen=True)
class Answer:
    question_id: int
    chosen_index: int | None  # None = 답하지 않음(오답으로 센다)
    elapsed_ms: int | None


@dataclass(frozen=True)
class ReaskAnswer:
    """재질문 양식의 한 행. reask_choice는 '원래' 보기 순서로 되돌려 둔 값이다."""
    attempt_id: int
    is_decoy: bool
    reask_choice: int | None
    reask_confident: bool | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            from misslens.paths import ledger_path

            path = ledger_path()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # 연결 하나를 여러 스레드(Streamlit, 생성기 스레드)가 공유하고, lock으로 직렬화한다.
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
        """ledger 패키지(stats)용 읽기 도우미. 비공개다: rule 1에 따라 SQL은 misslens.ledger 안에만 둔다."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- 세트와 문제 ---------------------------------------------------------------------

    def create_set(self, exam_id: str, size: int) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO quiz_sets (exam_id, size, status, created_at) VALUES (?,?, 'generating', ?)",
                            (exam_id, size, _now()))
            return int(cur.lastrowid)

    def start_set(self, set_id: int) -> None:
        """첫 문제를 보여 줄 때: generating -> in_progress(이미 in_progress면 아무것도 하지 않음)."""
        with self._tx() as c:
            status = self._set_status(c, set_id)
            if status == "generating":
                c.execute("UPDATE quiz_sets SET status='in_progress' WHERE id=?", (set_id,))
            elif status != "in_progress":
                raise SetStateError(f"set {set_id} is {status}; cannot start")

    def add_question(self, set_id: int, *, seq: int, task_id: str, domain: str, blueprint_version: int,
                     payload: dict[str, Any] | None, status: str = "ready") -> int:
        """검증을 '통과한' 문제(status ready)나 실패한 자리(§4.1)를 저장한다.

        Rule 2: validator나 근거 검증에서 떨어진 문제로는 절대 호출하지 않는다.
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
        """모든 답을 한 트랜잭션에서 채점하고 저장한다. attempt id를 입력 순서대로 돌려준다.

        `answers`는 세트의 ready 문제를 빠짐없이 담아야 한다. 답하지 않은 문제는 chosen_index=None으로 넘겨,
        통계에서 조용히 사라지지 않고 오답으로 세어지게 한다.
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
            c.execute("DELETE FROM draft_answers WHERE set_id=?", (set_id,))
            return ids

    def record_review(self, set_id: int, reasks: list[ReaskAnswer], *, reask_skipped: bool = False) -> None:
        """§4.2 표를 적용해 리뷰를 한 트랜잭션에서 저장한다.

        세트의 유효한 오답은 모두 포함해야 한다. 미끼는 정답을 맞힌 풀이여야 한다.
        reask_skipped=True(시험 언어와 학습자 언어가 같음)이면 오답을 reask_choice=None으로 넘긴다.
        이들은 모두 concept로 저장하며 미끼는 허용하지 않는다.
        """
        with self._tx() as c:
            cur = c.execute("UPDATE quiz_sets SET status='reviewed', reviewed_at=? WHERE id=? AND status='submitted'",
                            (_now(), set_id))
            if cur.rowcount != 1:
                raise SetStateError(f"set {set_id} is {self._set_status(c, set_id)}; cannot review")
            rows = {r["id"]: r for r in c.execute(
                "SELECT a.id, a.chosen_index, a.is_correct, a.invalid, q.payload_json FROM attempts a "
                "JOIN questions q ON q.id = a.question_id WHERE a.set_id=?", (set_id,))}
            # 이미 '문제 이상'으로 표시된 오답은 다시 묻지 않는다(모든 통계에서 빠져 있다).
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
        """학습자가 기록된 문구를 고친다. 문구는 reading 실수에만 있다(§4.2, rule 3)."""
        with self._tx() as c:
            row = c.execute("SELECT miss_type FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise LedgerError(f"no attempt {attempt_id}")
            if row["miss_type"] != "reading":
                raise LedgerError(f"attempt {attempt_id} is {row['miss_type']!r}; only reading misses have a phrase")
            c.execute("INSERT INTO missed_phrases (attempt_id, phrase, user_corrected) VALUES (?,?,1) "
                      "ON CONFLICT(attempt_id) DO UPDATE SET phrase=excluded.phrase, user_corrected=1",
                      (attempt_id, phrase))

    def record_miss_feedback(self, attempt_id: int, label: str, *, stage: str = "before_explanation") -> None:
        """리뷰한 오답에 대한 학습자 자신의 판단. 진단을 평가하는 데 쓴다(T10 eval, D43).

        miss_type과 따로 저장한다: 진단의 출처는 여전히 판정 표뿐이다(rule 3).
        정답을 보여 준 뒤, 판정과 해설을 보여 주기 전에 묻는다. 그래서 판정과 해설에 영향받지 않는다.
        """
        if label not in ("concept", "reading", "unsure"):
            raise LedgerError(f"unknown label {label!r}")
        with self._tx() as c:
            row = c.execute("SELECT is_correct, is_decoy, miss_type FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise LedgerError(f"no attempt {attempt_id}")
            if row["is_correct"] or row["miss_type"] is None:
                raise LedgerError(f"attempt {attempt_id} is not a reviewed miss")
            c.execute("INSERT INTO miss_feedback (attempt_id, learner_label, stage, created_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(attempt_id) DO UPDATE SET learner_label=excluded.learner_label, "
                      "stage=excluded.stage, created_at=excluded.created_at", (attempt_id, label, stage, _now()))

    def labeled_misses(self, exam_id: str) -> list[dict[str, Any]]:
        """학습자 피드백이 붙은 리뷰한 오답을 문제와 함께 돌려준다. eval 내보내기용이다(무효 풀이 제외)."""
        out = []
        for r in self._query(
                "SELECT a.id AS attempt_id, a.chosen_index, a.miss_type, m.phrase AS missed_phrase, f.learner_label,"
                " f.stage, f.created_at, q.task_id, q.domain, q.payload_json FROM miss_feedback f"
                " JOIN attempts a ON a.id = f.attempt_id JOIN questions q ON q.id = a.question_id"
                " LEFT JOIN missed_phrases m ON m.attempt_id = a.id"
                " WHERE q.exam_id = ? AND a.invalid = 0 ORDER BY a.id", (exam_id,)):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def mark_invalid(self, attempt_id: int, invalid: bool = True) -> None:
        """'문제 이상'(F8): 이 풀이를 문구 순위를 포함한 모든 통계에서 뺀다."""
        with self._tx() as c:
            if c.execute("UPDATE attempts SET invalid=? WHERE id=?", (int(invalid), attempt_id)).rowcount != 1:
                raise LedgerError(f"no attempt {attempt_id}")

    # --- 자료와 실패 기록 ---------------------------------------------------------------

    def add_material(self, exam_id: str, *, source_type: str, content_hash: str,
                     source_uri: str | None = None, task_id: str | None = None) -> int | None:
        """새 id를 돌려준다. 이 시험에 같은 내용이 이미 저장되어 있으면 None을 돌려준다."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO materials (exam_id, source_type, source_uri, content_hash, task_id,"
                            " collected_at) VALUES (?,?,?,?,?,?)",
                            (exam_id, source_type, source_uri, content_hash, task_id, _now()))
            return int(cur.lastrowid) if cur.rowcount == 1 else None

    def remove_materials(self, exam_id: str, source_uris: list[str]) -> int:
        """chunk가 색인에서 지워진 자료 행을 잊는다(예: 다시 정리한 수집본, D48)."""
        if not source_uris:
            return 0
        with self._tx() as c:
            marks = ",".join("?" * len(source_uris))
            cur = c.execute(f"DELETE FROM materials WHERE exam_id=? AND source_uri IN ({marks})",
                            (exam_id, *source_uris))
            return cur.rowcount

    # --- 계획, 임시 답안, 열린 세트(D52) ---------------------------------------------------
    # 통계가 아니다: 계획은 학습자가 승인한 것이고, 임시 답안은 아직 제출하지 않은 답이다. submit_set이 임시 답안을
    # attempts로 바꾸기 전까지는 어느 쪽도 어떤 숫자에도 반영되지 않는다(rule 1).

    def record_plan(self, exam_id: str, *, set_id: int | None, kind: str, total: int, counts: dict | None,
                    rationale: str = "", source: str = "", thread_id: str | None = None) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO plans (exam_id, set_id, kind, total, counts_json, rationale, source, thread_id,"
                            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (exam_id, set_id, kind, total, json.dumps(counts) if counts else None, rationale, source,
                             thread_id, _now()))
            return int(cur.lastrowid)

    def plan_for_set(self, set_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM plans WHERE set_id=? ORDER BY id DESC LIMIT 1", (set_id,))
        return self._plan_row(rows[0]) if rows else None

    def list_plans(self, exam_id: str, *, kind: str = "plan", limit: int = 20) -> list[dict[str, Any]]:
        """최신순으로, 연결된 세트의 상태와 진행도를 함께 돌려준다."""
        rows = self._query("SELECT * FROM plans WHERE exam_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                           (exam_id, kind, limit))
        return [self._plan_row(r) for r in rows]

    def _plan_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["counts"] = json.loads(d.pop("counts_json")) if d["counts_json"] else None
        d["set"] = self.set_progress(d["set_id"]) if d["set_id"] else None
        return d

    def set_progress(self, set_id: int) -> dict[str, Any]:
        s = self.get_set(set_id)
        n = lambda sql: self._query(sql, (set_id,))[0][0]  # noqa: E731
        return {"id": set_id, "status": s["status"], "size": s["size"], "created_at": s["created_at"],
                "ready": n("SELECT count(*) FROM questions WHERE set_id=? AND status='ready'"),
                "failed": n("SELECT count(*) FROM questions WHERE set_id=? AND status='failed'"),
                "drafts": n("SELECT count(*) FROM draft_answers WHERE set_id=?")}

    def list_sets(self, exam_id: str, *, statuses: tuple[str, ...] = ("submitted", "reviewed"),
                  limit: int = 30) -> list[dict[str, Any]]:
        """주어진 상태의 세트를 최신순으로, 진행도와 만들어진 방식과 함께 돌려준다(D53).
        origin: plan | quick | ask(자유 입력 요청) | cli | unknown(계획을 기록하기 전에 만든 세트)."""
        marks = ",".join("?" * len(statuses))
        rows = self._query(f"SELECT id FROM quiz_sets WHERE exam_id=? AND status IN ({marks}) ORDER BY id DESC LIMIT ?",
                           (exam_id, *statuses, limit))
        out = []
        for r in rows:
            d = self.set_progress(r["id"])
            plan = self._query("SELECT kind, thread_id, counts_json FROM plans WHERE set_id=? ORDER BY id DESC LIMIT 1",
                               (r["id"],))
            if not plan:
                d["origin"], d["counts"] = "unknown", None
            else:
                kind, tid = plan[0]["kind"], plan[0]["thread_id"] or ""
                d["origin"] = ("plan" if kind == "plan" else "ask" if tid.startswith("ui-ask")
                               else "cli" if tid.startswith("cli") else "quick")
                d["counts"] = json.loads(plan[0]["counts_json"]) if plan[0]["counts_json"] else None
            out.append(d)
        return out

    def open_sets(self, exam_id: str) -> list[dict[str, Any]]:
        """아직 제출하지 않은 세트(generating / in_progress)를 최신순으로, 진행도와 함께 돌려준다."""
        rows = self._query("SELECT id FROM quiz_sets WHERE exam_id=? AND status IN ('generating','in_progress')"
                           " ORDER BY id DESC", (exam_id,))
        return [self.set_progress(r["id"]) for r in rows]

    def save_draft_answer(self, set_id: int, question_id: int, chosen_index: int | None,
                          elapsed_ms: int | None) -> None:
        with self._tx() as c:
            if self._set_status(c, set_id) not in ("generating", "in_progress"):
                raise SetStateError(f"set {set_id} is already submitted")
            row = c.execute("SELECT set_id FROM questions WHERE id=? AND status='ready'", (question_id,)).fetchone()
            if row is None or row["set_id"] != set_id:
                raise LedgerError(f"question {question_id} is not a ready question of set {set_id}")
            c.execute("INSERT INTO draft_answers (question_id, set_id, chosen_index, elapsed_ms, updated_at)"
                      " VALUES (?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET chosen_index=excluded.chosen_index,"
                      " elapsed_ms=excluded.elapsed_ms, updated_at=excluded.updated_at",
                      (question_id, set_id, chosen_index, elapsed_ms, _now()))

    def draft_answers(self, set_id: int) -> dict[int, dict[str, Any]]:
        return {r["question_id"]: dict(r) for r in self._query(
            "SELECT question_id, chosen_index, elapsed_ms FROM draft_answers WHERE set_id=?", (set_id,))}

    # --- 단어장(D54) -------------------------------------------------------------------------

    def save_vocab(self, exam_id: str, set_id: int, items: list[dict[str, Any]]) -> None:
        """세트의 단어 목록을 교체한다(items는 agents.grader.vocab.clean_items에서 온다)."""
        with self._tx() as c:
            c.execute("DELETE FROM vocab WHERE set_id=?", (set_id,))
            for it in items:
                c.execute("INSERT INTO vocab (exam_id, set_id, question_id, term, term_key, kind, meaning, role, from_miss,"
                          " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (exam_id, set_id, it["question_id"], it["term"], it["term_key"], it["kind"], it["meaning"],
                           it.get("role"), int(bool(it.get("from_miss"))), _now()))

    def set_vocab(self, set_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT v.*, q.seq, q.payload_json, (k.term_key IS NOT NULL) AS known FROM vocab v"
            " JOIN questions q ON q.id = v.question_id"
            " LEFT JOIN vocab_known k ON k.exam_id = v.exam_id AND k.term_key = v.term_key"
            " WHERE v.set_id=? ORDER BY v.from_miss DESC, q.seq, v.id", (set_id,))]

    def vocab_book(self, exam_id: str) -> list[dict[str, Any]]:
        """모든 세트에 걸친 표현 전체: 최신 뜻, 나온 횟수, 틀린 문제에서 나왔는지, 아는 단어 표시.
        틀린 문제에서 나온 것 먼저, 그다음 자주 나온 순서다(횟수는 여기서 계산한다, rule 1)."""
        rows = self._query(
            "SELECT v.term_key, count(*) AS seen, count(DISTINCT v.set_id) AS sets, max(v.from_miss) AS from_miss,"
            " max(v.id) AS last_id, (k.term_key IS NOT NULL) AS known FROM vocab v"
            " LEFT JOIN vocab_known k ON k.exam_id = v.exam_id AND k.term_key = v.term_key"
            " WHERE v.exam_id=? GROUP BY v.term_key ORDER BY from_miss DESC, seen DESC, last_id DESC", (exam_id,))
        out = []
        for r in rows:
            last = self._query("SELECT term, kind, meaning, role FROM vocab WHERE id=?", (r["last_id"],))[0]
            uses = self._query("SELECT v.set_id, q.seq, q.payload_json FROM vocab v JOIN questions q ON q.id=v.question_id"
                               " WHERE v.exam_id=? AND v.term_key=? ORDER BY v.id DESC LIMIT 3", (exam_id, r["term_key"]))
            out.append({**dict(last), "term_key": r["term_key"], "seen": r["seen"], "sets": r["sets"],
                        "from_miss": bool(r["from_miss"]), "known": bool(r["known"]),
                        "uses": [{"set_id": u["set_id"], "seq": u["seq"], "stem_en": json.loads(u["payload_json"])["stem_en"]}
                                 for u in uses]})
        return out

    def mark_known(self, exam_id: str, term_key: str, known: bool = True) -> None:
        with self._tx() as c:
            if known:
                c.execute("INSERT OR IGNORE INTO vocab_known (exam_id, term_key, known_at) VALUES (?,?,?)",
                          (exam_id, term_key, _now()))
            else:
                c.execute("DELETE FROM vocab_known WHERE exam_id=? AND term_key=?", (exam_id, term_key))

    def record_generation_failure(self, exam_id: str, task_id: str, tries: int, reason: str) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO generation_failures (exam_id, task_id, tries, reason, created_at)"
                            " VALUES (?,?,?,?,?)", (exam_id, task_id, tries, reason, _now()))
            return int(cur.lastrowid)

    # --- 핵심 루프용 읽기 ---------------------------------------------------------------

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
