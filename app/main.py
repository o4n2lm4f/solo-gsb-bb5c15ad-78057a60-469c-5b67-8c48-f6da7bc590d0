"""FastAPI application exposing the local dependency resolver.

Two execution styles share one resolution pipeline (:mod:`app.service`):

* ``POST /resolve`` — synchronous resolution, unchanged.
* ``POST /jobs``     — persist the same request as a ``queued`` job; a
  background worker claims and resolves it, ``GET /jobs/{id}`` polls state
  and the final response, and ``DELETE /jobs/{id}`` cancels it.

Job state is persisted in SQLite so it survives restarts; on startup jobs
left ``running`` by a previous process are returned to the queue.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .resolver import ResolutionInputError
from .service import ResolveRequest, extract_and_resolve
from .store import JobStore
from .worker import JobRunner


def _db_path() -> str:
    return os.environ.get("RESOLVER_DB_PATH", "data/resolver.db")


def _worker_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("JOB_WORKERS", "1")))
    except ValueError:
        return 1


def _worker_poll_interval() -> float:
    try:
        return max(0.01, float(os.environ.get("JOB_POLL_INTERVAL", "0.1")))
    except ValueError:
        return 0.1


# The store is created eagerly (after env configuration) so importing the
# module for tests is enough; the lifespan additionally performs crash
# recovery and starts the background worker pool.
store = JobStore(_db_path())
runner = JobRunner(
    store,
    concurrency=_worker_concurrency(),
    poll_interval=_worker_poll_interval(),
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Crash recovery: a previous process may have died while jobs were
    # running.  Put them back on the queue.
    await asyncio.to_thread(store.requeue_stale_running)
    runner.start()
    try:
        yield
    finally:
        await runner.shutdown()


app = FastAPI(
    title="Local PEP 440 Dependency Resolver",
    version="1.1.0",
    description=(
        "Resolves transitive dependencies from a self-contained package index. "
        "No external package index or account is used. "
        "POST /resolve resolves synchronously; POST /jobs runs the same "
        "request as an asynchronous, persisted job."
    ),
    lifespan=lifespan,
)


@app.exception_handler(ResolutionInputError)
async def _input_error_handler(_request: Request, exc: ResolutionInputError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": "invalid_request", "message": str(exc)}})


# ---------------------------------------------------------------------------
# Synchronous API (unchanged)
# ---------------------------------------------------------------------------


@app.post("/resolve")
async def resolve_endpoint(payload: ResolveRequest) -> JSONResponse:
    # The payload has already passed FastAPI/pydantic validation (422 on
    # structural problems), so re-validating the dumped model cannot fail;
    # semantic errors (bad PEP 440 versions, bad specifiers) map to 400.
    status_code, body = await asyncio.to_thread(
        extract_and_resolve, payload.model_dump(mode="json")
    )
    return JSONResponse(status_code=status_code, content=body)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Asynchronous job API
# ---------------------------------------------------------------------------


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Public representation of a persisted job."""
    body: dict[str, Any] = {
        "id": str(job["id"]),
        "status": job["status"],
        "request": job["request"],
        "cancel_requested": job["cancel_requested"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    if "response" in job:
        body["response_status"] = job["response_status"]
        body["response"] = job["response"]
    return body


@app.post("/jobs", status_code=202)
async def create_job(request: Request) -> JSONResponse:
    """Persist a resolve request as a queued job (202 Accepted)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "request body must be valid JSON"}},
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "request body must be a JSON object"}},
        )
    job = await asyncio.to_thread(store.enqueue, payload)
    return JSONResponse(status_code=202, content=_job_view(job))


@app.get("/jobs/{job_id}")
async def get_job(job_id: int) -> JSONResponse:
    job = await asyncio.to_thread(store.get, job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "job_not_found", "message": f"no job with id {job_id}"}},
        )
    return JSONResponse(status_code=200, content=_job_view(job))


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: int) -> JSONResponse:
    """Cancel a job.

    * ``queued``  -> ``cancelled`` immediately (200).
    * ``running`` -> a cooperative stop is requested at the next candidate
      evaluation boundary (202 Accepted); the job ends as ``cancelled``.
    * terminal jobs (``succeeded`` / ``failed`` / ``cancelled``) are never
      modified (409 Conflict).
    """
    result = await asyncio.to_thread(store.request_cancel, job_id)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "job_not_found", "message": f"no job with id {job_id}"}},
        )
    status, changed = result
    runner.signal_cancel(job_id)
    job = await asyncio.to_thread(store.get, job_id)
    assert job is not None
    if changed:
        # queued -> cancelled happened synchronously.
        return JSONResponse(status_code=200, content=_job_view(job))
    if status == "running":
        return JSONResponse(status_code=202, content=_job_view(job))
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "job_terminal",
                "message": f"job {job_id} is already in terminal state {status!r} and cannot be cancelled",
            }
        },
    )
