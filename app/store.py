"""SQLite persistence for asynchronous resolution jobs.

Job lifecycle::

    queued ──claim──▶ running ──▶ succeeded
       │                │
       │                ├────────▶ failed          (400/409 resolution result)
       │                └────────▶ cancelled       (cooperative stop)
       └──cancel──────▶ cancelled

Terminal states (``succeeded`` / ``failed`` / ``cancelled``) are immutable:
every guarded transition is a no-op once the row leaves its active state.

Claims use ``BEGIN IMMEDIATE`` so that multiple workers (or a worker racing
with a cancel request) get a single resolved decision per transaction; the
rowid ordering picks the oldest ``queued`` job first (FIFO).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    status           TEXT    NOT NULL,
    request_json     TEXT    NOT NULL,
    response_status  INTEGER,
    response_json    TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
)
"""

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Thread-safe SQLite wrapper. One shared instance per process."""

    def __init__(self, path: str):
        self.path = path
        # A single guarded connection is plenty for this workload and keeps
        # transaction semantics (BEGIN IMMEDIATE ... COMMIT) local to a call.
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def requeue_stale_running(self) -> int:
        """Return jobs left ``running`` by a previous process to the queue.

        Workers persist ``running`` immediately after claiming; a crash (or
        ``docker compose restart``) therefore leaves rows stuck in that
        state.  On startup they become runnable again.  A cancellation that
        landed before the crash sticks: the worker claims such a row and
        finalizes it as ``cancelled`` without running the solver.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL "
                "WHERE status = 'running'"
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Enqueue / claim
    # ------------------------------------------------------------------

    def enqueue(self, request: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (status, request_json, created_at) "
                "VALUES ('queued', ?, ?)",
                (json.dumps(request, separators=(",", ":"), ensure_ascii=False), now),
            )
            job_id = int(cur.lastrowid)
        row = self.get(job_id)
        assert row is not None
        return row

    def claim_next(self) -> Optional[dict[str, Any]]:
        """Atomically claim the oldest queued job.

        ``BEGIN IMMEDIATE`` takes the write lock before the SELECT, so two
        workers can never claim the same row.  A queued job whose cancellation
        already landed is finalized as ``cancelled`` right here (sticky
        cancel), and claiming continues to the next row.
        """
        with self._lock:
            while True:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    row = self._conn.execute(
                        "SELECT id, cancel_requested FROM jobs "
                        "WHERE status = 'queued' ORDER BY id LIMIT 1"
                    ).fetchone()
                    if row is None:
                        self._conn.execute("COMMIT")
                        return None
                    job_id = row["id"]
                    now = _utcnow()
                    if row["cancel_requested"]:
                        self._conn.execute(
                            "UPDATE jobs SET status = 'cancelled', "
                            "finished_at = ? WHERE id = ? AND status = 'queued'",
                            (now, job_id),
                        )
                        self._conn.execute("COMMIT")
                        continue
                    self._conn.execute(
                        "UPDATE jobs SET status = 'running', started_at = ? "
                        "WHERE id = ? AND status = 'queued'",
                        (now, job_id),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                claimed = self.get(job_id)
                assert claimed is not None
                return claimed

    # ------------------------------------------------------------------
    # Transitions (all guarded against overwriting terminal states)
    # ------------------------------------------------------------------

    def finish_with_response(self, job_id: int, status_code: int, body: dict[str, Any]) -> bool:
        """Record a finished resolution (ok/conflict/limit/invalid request).

        Returns ``True`` when the transition happened, ``False`` when the job
        was already cancelled (or otherwise terminal).
        """
        kind = "succeeded" if status_code == 200 else "failed"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, response_status = ?, response_json = ?, "
                "finished_at = ? WHERE id = ? AND status = 'running'",
                (
                    kind,
                    status_code,
                    json.dumps(body, separators=(",", ":"), ensure_ascii=False),
                    _utcnow(),
                    job_id,
                ),
            )
            return cur.rowcount > 0

    def cancel_running(self, job_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_utcnow(), job_id),
            )
            return cur.rowcount > 0

    def request_cancel(self, job_id: int) -> Optional[tuple[str, bool]]:
        """Ask for a job to be cancelled.

        Returns ``(status, changed)`` where ``changed`` tells whether this
        call performed the (terminal) queued -> cancelled transition.
        Returns ``None`` when the job does not exist.  Terminal jobs are
        never modified.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                status = row["status"]
                changed = False
                if status == "queued":
                    now = _utcnow()
                    cur = self._conn.execute(
                        "UPDATE jobs SET status = 'cancelled', "
                        "cancel_requested = 1, finished_at = ? "
                        "WHERE id = ? AND status = 'queued'",
                        (now, job_id),
                    )
                    changed = cur.rowcount > 0
                    status = "cancelled" if changed else status
                elif status == "running":
                    self._conn.execute(
                        "UPDATE jobs SET cancel_requested = 1 "
                        "WHERE id = ? AND status = 'running'",
                        (job_id,),
                    )
                # terminal states: immutable, nothing to do
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return status, changed

    def is_cancel_requested(self, job_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    # ------------------------------------------------------------------
    # Reads / admin
    # ------------------------------------------------------------------

    def get(self, job_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        job: dict[str, Any] = {
            "id": row["id"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if row["response_json"] is not None:
            job["response_status"] = row["response_status"]
            job["response"] = json.loads(row["response_json"])
        return job

    def clear(self) -> None:
        """Test helper: drop every job row."""
        with self._lock:
            self._conn.execute("DELETE FROM jobs")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
