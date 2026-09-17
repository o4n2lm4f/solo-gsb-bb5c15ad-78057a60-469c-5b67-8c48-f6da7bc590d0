"""End-to-end tests for the asynchronous /jobs API."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app, store

RESOLVABLE = {
    "packages": {
        "app": {"1.0": {"dependencies": {"lib": ">=1.0"}}},
        "lib": {"1.0": {}, "2.0": {}},
    },
    "root": {"app": ""},
}

CONFLICT = {
    "packages": {
        "a": {"1.0": {"dependencies": {"x": ">=2.0"}}},
        "b": {"1.0": {"dependencies": {"x": "<2.0"}}},
        "x": {"1.0": {}, "2.0": {}},
    },
    "root": ["a", "b"],
}


@pytest.fixture(autouse=True)
def _empty_jobs():
    store.clear()
    yield
    store.clear()


def _wait_for(client: TestClient, job_id: str, timeout: float = 5.0, **expected):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(f"/jobs/{job_id}").json()
        if all(body.get(k) == v for k, v in expected.items()):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} never reached {expected}: {body}")
        time.sleep(0.01)


def _enqueue(client: TestClient, payload=RESOLVABLE):
    response = client.post("/jobs", json=payload)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    return body["id"], body


def test_create_job_is_accepted_and_persists_request():
    with TestClient(app) as client:
        job_id, body = _enqueue(client)
        assert body["request"] == RESOLVABLE
        assert body["cancel_requested"] is False
        assert body["started_at"] is None
        assert body["finished_at"] is None
        assert body["created_at"]
        assert "response" not in body

        fetched = client.get(f"/jobs/{job_id}").json()
        assert fetched["id"] == job_id
        assert fetched["request"] == RESOLVABLE


def test_job_runs_to_succeeded_and_returns_final_response():
    with TestClient(app) as client:
        job_id, _ = _enqueue(client)
        body = _wait_for(client, job_id, status="succeeded")
        assert body["response_status"] == 200
        assert body["response"]["status"] == "ok"
        assert body["response"]["resolved"] == {"app": "1.0", "lib": "2.0"}
        assert body["started_at"] and body["finished_at"]


def test_conflict_job_is_failed_not_succeeded():
    with TestClient(app) as client:
        job_id, _ = _enqueue(client, CONFLICT)
        body = _wait_for(client, job_id, status="failed")
        assert body["response_status"] == 409
        assert body["response"]["status"] == "conflict"


def test_invalid_request_job_fails_with_400_response():
    with TestClient(app) as client:
        response = client.post(
            "/jobs", json={"packages": {"x": {"nope": {}}}, "root": {"x": ""}}
        )
        assert response.status_code == 202  # validation happens in the worker
        job_id = response.json()["id"]
        body = _wait_for(client, job_id, status="failed")
        assert body["response_status"] == 400
        assert body["response"]["error"]["code"] == "invalid_request"


def test_non_json_body_rejected_when_enqueuing():
    with TestClient(app) as client:
        response = client.post(
            "/jobs", content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_get_unknown_job_returns_404():
    with TestClient(app) as client:
        response = client.get("/jobs/999999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"


def test_cancel_queued_job_is_immediate():
    # Without the lifespan/worker running, jobs stay queued deterministically.
    client = TestClient(app)
    job_id, _ = _enqueue(client)
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"

    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancel_requested"] is True
    assert body["finished_at"]

    fetched = client.get(f"/jobs/{job_id}").json()
    assert fetched["status"] == "cancelled"
    assert "response" not in fetched


def test_cancel_running_job_stops_cooperatively_at_boundary(monkeypatch):
    from app import worker

    entered = threading.Event()
    release = threading.Event()

    original = worker.JobRunner._make_should_continue

    def patched(self, job_id):
        original_check = original(self, job_id)

        def should_continue():
            if not entered.is_set():
                entered.set()
                release.wait(timeout=5.0)
            return original_check()

        return should_continue

    monkeypatch.setattr(worker.JobRunner, "_make_should_continue", patched)

    with TestClient(app) as client:
        job_id, _ = _enqueue(client, CONFLICT)
        assert entered.wait(timeout=5.0)

        # The job is now executing its first candidate evaluation.
        running = client.get(f"/jobs/{job_id}").json()
        assert running["status"] == "running"

        response = client.delete(f"/jobs/{job_id}")
        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert response.json()["cancel_requested"] is True

        release.set()
        body = _wait_for(client, job_id, status="cancelled")
        assert "response" not in body
        assert body["finished_at"]


def test_terminal_job_cannot_be_overwritten():
    with TestClient(app) as client:
        job_id, _ = _enqueue(client)
        _wait_for(client, job_id, status="succeeded")

        response = client.delete(f"/jobs/{job_id}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "job_terminal"

        body = client.get(f"/jobs/{job_id}").json()
        assert body["status"] == "succeeded"
        assert body["response"]["status"] == "ok"


def test_delete_unknown_job_returns_404():
    with TestClient(app) as client:
        response = client.delete("/jobs/424242")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"


def test_jobs_are_claimed_fifo():
    # No worker running: claim directly through the store.
    client = TestClient(app)
    ids = [_enqueue(client)[0] for _ in range(3)]
    claimed = [str(store.claim_next()["id"]), str(store.claim_next()["id"]), str(store.claim_next()["id"])]
    assert claimed == ids
    assert store.claim_next() is None


def test_stale_running_jobs_are_requeued_on_startup():
    client = TestClient(app)
    job_id = int(_enqueue(client)[0])
    claimed = store.claim_next()
    assert claimed["status"] == "running"

    # Simulate a crash: a fresh store on the same database file recovers it.
    from app.store import JobStore

    recovered = JobStore(store.path)
    try:
        assert recovered.requeue_stale_running() == 1
        row = recovered.get(job_id)
        assert row["status"] == "queued"
        assert row["started_at"] is None
        reclaimed = recovered.claim_next()
        assert reclaimed["id"] == job_id and reclaimed["status"] == "running"
    finally:
        recovered.close()


def test_cancel_requested_before_crash_sticks_after_requeue():
    """A cancel that landed while queued must survive a crash/requeue cycle."""
    from app.store import JobStore

    client = TestClient(app)
    job_id = int(_enqueue(client)[0])
    status, changed = store.request_cancel(job_id)
    assert status == "cancelled" and changed is True
    # Terminal cancelled rows are not requeued.
    recovered = JobStore(store.path)
    try:
        assert recovered.requeue_stale_running() == 0
        assert recovered.get(job_id)["status"] == "cancelled"
    finally:
        recovered.close()


def test_sticky_cancel_for_running_then_requeued_job():
    """running job flagged for cancel, then crashed: claim finalizes cancelled."""
    from app.store import JobStore

    client = TestClient(app)
    job_id = int(_enqueue(client)[0])
    job = store.claim_next()
    assert job["status"] == "running"
    status, changed = store.request_cancel(job_id)
    assert status == "running" and changed is False

    recovered = JobStore(store.path)
    try:
        assert recovered.requeue_stale_running() == 1
        # claim_next atomically finalizes the cancel-flagged queued row and
        # reports no runnable job.
        assert recovered.claim_next() is None
        assert recovered.get(job_id)["status"] == "cancelled"
        assert recovered.claim_next() is None
    finally:
        recovered.close()


def test_finish_does_not_overwrite_cancelled_job():
    client = TestClient(app)
    job_id = int(_enqueue(client)[0])
    store.claim_next()
    # Cancel via store-level transition (race: cancel wins before finish).
    assert store.cancel_running(job_id) is True
    assert store.finish_with_response(job_id, 200, {"status": "ok"}) is False
    job = store.get(job_id)
    assert job["status"] == "cancelled"
    assert "response" not in job


def test_job_response_matches_synchronous_resolve():
    with TestClient(app) as client:
        job_id, _ = _enqueue(client)
        async_body = _wait_for(client, job_id, status="succeeded")["response"]
        sync = client.post("/resolve", json=RESOLVABLE)
        assert sync.status_code == 200
        assert sync.json() == async_body


def test_jobs_persist_across_store_restart():
    """Queued and finished rows survive closing and reopening the database."""
    from app.store import JobStore

    client = TestClient(app)
    queued_id = int(_enqueue(client)[0])
    finished_id = int(_enqueue(client)[0])
    job = store.claim_next()  # claims queued_id (FIFO); simulate finished_id too
    second = store.claim_next()
    assert second["id"] == finished_id
    assert store.finish_with_response(
        finished_id, 200, {"status": "ok", "resolved": {"app": "1.0"}}
    )

    reopened = JobStore(store.path)
    try:
        assert reopened.get(queued_id)["status"] == "running"
        finished = reopened.get(finished_id)
        assert finished["status"] == "succeeded"
        assert finished["response"] == {"status": "ok", "resolved": {"app": "1.0"}}
    finally:
        reopened.close()


def test_structurally_invalid_payload_job_fails_with_400():
    with TestClient(app) as client:
        response = client.post("/jobs", json={"packages": {}})  # missing root
        assert response.status_code == 202
        job_id = response.json()["id"]
        body = _wait_for(client, job_id, status="failed")
        assert body["response_status"] == 400
        assert "root" in body["response"]["error"]["message"]


def test_non_object_json_body_rejected_when_enqueuing():
    with TestClient(app) as client:
        response = client.post("/jobs", json=[1, 2, 3])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_concurrent_claims_never_double_assign():
    """BEGIN IMMEDIATE must make claims unique even across store instances."""
    from app.store import JobStore

    client = TestClient(app)
    n = 20
    for _ in range(n):
        _enqueue(client)

    other_stores = [JobStore(store.path) for _ in range(3)]
    all_stores = [store] + other_stores
    claimed: list[int] = []
    claim_lock = threading.Lock()

    def hammer(s: JobStore):
        for _ in range(n):
            job = s.claim_next()
            if job is not None:
                with claim_lock:
                    claimed.append(job["id"])

    threads = [threading.Thread(target=hammer, args=(s,)) for s in other_stores + [store]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    try:
        assert len(claimed) == n
        assert len(set(claimed)) == n
        for s in all_stores:
            assert s.claim_next() is None
    finally:
        for s in other_stores:
            s.close()


def test_idempotent_delete_on_cancelled_job_returns_409():
    client = TestClient(app)
    job_id, _ = _enqueue(client)
    assert client.delete(f"/jobs/{job_id}").status_code == 200
    again = client.delete(f"/jobs/{job_id}")
    assert again.status_code == 409
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"
