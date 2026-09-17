"""FastAPI application exposing the local dependency resolver."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .resolver import (
    ResolutionInputError,
    parse_index,
    parse_locks,
    parse_requirements,
    resolve,
)
from .serialize import conflict_summary

app = FastAPI(
    title="Local PEP 440 Dependency Resolver",
    version="1.0.0",
    description=(
        "Resolves transitive dependencies from a self-contained package index. "
        "No external package index or account is used."
    ),
)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packages: dict[str, dict[str, Any]] = Field(
        ...,
        description=(
            "Package index: package name -> version -> entry. "
            "Each entry may contain 'dependencies' as either "
            "{package: specifier} or a list of PEP 508 strings."
        ),
    )
    root: Any = Field(
        ...,
        description="Root requirements: {package: specifier} or a list of PEP 508 strings.",
    )
    locks: Optional[dict[str, str]] = Field(
        default=None,
        description="Optional pinned versions. Pins are kept while they satisfy all constraints.",
    )
    allow_prereleases: bool = Field(
        default=False,
        description="Include a/b/rc pre-releases for every package when true.",
    )
    marker_env: Optional[dict[str, str]] = Field(
        default=None,
        description="Override values for PEP 508 environment marker evaluation.",
    )
    max_attempts: int = Field(
        default=100_000, ge=1, le=10_000_000,
        description="Bound on candidate evaluations to keep resolution finite.",
    )


@app.exception_handler(ResolutionInputError)
async def _input_error_handler(_request: Request, exc: ResolutionInputError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": "invalid_request", "message": str(exc)}})


def _extract_and_resolve(payload: ResolveRequest) -> dict[str, Any]:
    index, index_errors = parse_index(payload.packages)
    if index_errors:
        raise ResolutionInputError("; ".join(index_errors))

    try:
        roots = parse_requirements(payload.root, "root requirements")
    except ResolutionInputError as exc:
        raise ResolutionInputError(str(exc))

    locks, lock_errors = parse_locks(payload.locks or {})
    if lock_errors:
        raise ResolutionInputError("; ".join(lock_errors))

    result = resolve(
        index,
        roots,
        locks,
        allow_prereleases=payload.allow_prereleases,
        marker_env=payload.marker_env,
        max_attempts=payload.max_attempts,
    )

    body: dict[str, Any] = {
        "status": result.status,
        "attempts": result.attempts,
        "allow_prereleases": payload.allow_prereleases,
    }
    if result.status == "ok":
        body["resolved"] = result.chosen
        body["dependency_graph"] = result.edges
        body["locks"] = result.lock_notes
    elif result.status == "conflict":
        body["locks"] = result.lock_notes
        body["conflict"] = conflict_summary(result)
    else:  # limit
        body["locks"] = result.lock_notes
        body["conflict"] = conflict_summary(result)
        body["error"] = {
            "code": "search_limit_exceeded",
            "message": f"resolution exceeded {payload.max_attempts} candidate evaluations",
        }
    return body


@app.post("/resolve")
async def resolve_endpoint(payload: ResolveRequest) -> JSONResponse:
    body = _extract_and_resolve(payload)
    status_code = 200 if body["status"] == "ok" else 409
    return JSONResponse(status_code=status_code, content=body)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
