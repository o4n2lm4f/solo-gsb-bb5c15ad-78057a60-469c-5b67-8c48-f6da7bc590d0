"""Background worker pool that runs queued resolution jobs.

Each worker slot runs an asyncio loop that atomically claims the oldest
queued job (``BEGIN IMMEDIATE``, see :mod:`app.store`) and executes the
resolver in a worker thread so the event loop stays responsive.

Cancellation is cooperative and checked by the resolver at every
candidate-evaluation boundary.  A ``threading.Event`` gives same-process
DELETE requests immediate wake-ups; the persisted ``cancel_requested`` flag
remains the source of truth and is polled periodically so cancels issued by
another process (or after a restart) are honoured too.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from .resolver import ResolutionCancelled
from .service import extract_and_resolve
from .store import JobStore

# Re-read the persisted cancel flag from SQLite every N boundary checks.
# The in-process event normally fires instantly; this only matters for
# cancels arriving from another process sharing the database.
_DB_POLL_EVERY = 16


class JobRunner:
    def __init__(self, store: JobStore, concurrency: int = 1, poll_interval: float = 0.1):
        self.store = store
        self.concurrency = max(1, concurrency)
        self.poll_interval = poll_interval
        # Created in start() so each lifespan binds its primitives to the
        # currently running event loop (tests create many short-lived loops;
        # production has a single lifespan per process).
        self._stop: Optional[asyncio.Event] = None
        self._tasks: list[asyncio.Task[None]] = []
        self._cancel_events: dict[int, threading.Event] = {}
        self._events_lock = threading.Lock()

    # -- cancellation signalling --------------------------------------

    def _event_for(self, job_id: int) -> threading.Event:
        with self._events_lock:
            event = self._cancel_events.get(job_id)
            if event is None:
                event = threading.Event()
                self._cancel_events[job_id] = event
            return event

    def signal_cancel(self, job_id: int) -> None:
        """Wake a running job's boundary check immediately (best effort)."""
        with self._events_lock:
            event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()

    def _make_should_continue(self, job_id: int) -> Any:
        event = self._event_for(job_id)
        counter = {"n": 0}

        def should_continue() -> bool:
            if event.is_set():
                return False
            counter["n"] += 1
            if counter["n"] % _DB_POLL_EVERY == 0:
                return not self.store.is_cancel_requested(job_id)
            return True

        return should_continue

    # -- job execution -------------------------------------------------

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        try:
            # A cancel may have landed between the claim and this thread
            # starting; check once before spending any work.
            if self.store.is_cancel_requested(job_id):
                self.store.cancel_running(job_id)
                return
            status_code, body = extract_and_resolve(
                job["request"],
                should_continue=self._make_should_continue(job_id),
            )
            self.store.finish_with_response(job_id, status_code, body)
        except ResolutionCancelled:
            self.store.cancel_running(job_id)
        except Exception as exc:  # defensive: never let the thread die silently
            self.store.finish_with_response(
                job_id,
                500,
                {"error": {"code": "internal_error", "message": str(exc)}},
            )
        finally:
            with self._events_lock:
                self._cancel_events.pop(job_id, None)

    async def _worker_loop(self) -> None:
        assert self._stop is not None
        stop = self._stop
        while not stop.is_set():
            job = await asyncio.to_thread(self.store.claim_next)
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            await asyncio.to_thread(self._run_job, job)

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._tasks:
            return
        self._stop = asyncio.Event()
        for _ in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._worker_loop()))

    async def shutdown(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
        self._stop = None
