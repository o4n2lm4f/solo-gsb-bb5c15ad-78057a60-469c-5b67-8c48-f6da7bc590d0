"""Shared resolution pipeline used by both the sync and job-based APIs."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .resolver import (
    ResolutionInputError,
    parse_index,
    parse_locks,
    parse_requirements,
    resolve,
)
from .serialize import conflict_summary


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


def _request_error(message: str) -> tuple[int, dict[str, Any]]:
    return 400, {"error": {"code": "invalid_request", "message": message}}


def extract_and_resolve(
    payload: Any,
    *,
    should_continue: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Validate a raw JSON payload and run the resolver.

    Returns ``(http_status, response_body)``.  Structural problems rejected by
    pydantic and semantic problems rejected while parsing the index/locks are
    both surfaced as ``400`` here; the synchronous endpoint simply forwards the
    pair, while a job persists it as the final response.
    """
    if not isinstance(payload, dict):
        return _request_error("request body must be a JSON object")

    try:
        model = ResolveRequest.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        return _request_error(details)

    try:
        index, index_errors = parse_index(model.packages)
        if index_errors:
            raise ResolutionInputError("; ".join(index_errors))

        try:
            roots = parse_requirements(model.root, "root requirements")
        except ResolutionInputError as exc:
            raise ResolutionInputError(str(exc))

        locks, lock_errors = parse_locks(model.locks or {})
        if lock_errors:
            raise ResolutionInputError("; ".join(lock_errors))

        result = resolve(
            index,
            roots,
            locks,
            allow_prereleases=model.allow_prereleases,
            marker_env=model.marker_env,
            max_attempts=model.max_attempts,
            should_continue=should_continue,
        )
    except ResolutionInputError as exc:
        return _request_error(str(exc))

    body: dict[str, Any] = {
        "status": result.status,
        "attempts": result.attempts,
        "allow_prereleases": model.allow_prereleases,
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
            "message": f"resolution exceeded {model.max_attempts} candidate evaluations",
        }
    return (200 if result.status == "ok" else 409, body)
