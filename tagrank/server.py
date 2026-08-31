"""TagRank's HTTP API. Run via `python main.py --serve [--port N]` (see tagrank/app.py).

See docs/api.md for an integration walkthrough. Every route below also shows up, fully
documented and "try it out"-able, at /docs (Swagger UI) once the server is running.

Design notes:
  - Only /sessions (POST) is asynchronous: it does a live Hydrus similarity search that can
    take anywhere from under a second to tens of seconds, so it returns a job id immediately
    and the caller polls GET /sessions/{job_id} until the session is ready. Every other route
    is a fast, synchronous in-memory operation on an existing RatingSystem.
  - There is no auth: this is meant to be run as a localhost-only subprocess spawned by the
    app that wants to use it (e.g. via `python main.py --serve`), not exposed on a network.
"""

import asyncio
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from tagrank import service
from tagrank.errors import TagRankError
from tagrank.service import NoPairAvailableError, Session, SessionNotFoundError

app = FastAPI(
    title="TagRank API",
    description=(
        "Headless API for TagRank: launch rating-comparison sessions, judge file pairs, "
        "and read back tag data, prediction history, and summary charts. "
        "See docs/api.md in the tagrank repo for a full integration walkthrough."
    ),
    version="1.0.0",
)


# --------------------------------------------------------------------------------------
# Error mapping - every tagrank.errors.TagRankError becomes a structured HTTP response
# instead of the CLI's print-and-exit behavior.
# --------------------------------------------------------------------------------------

_ERROR_STATUS: dict[type[TagRankError], int] = {
    SessionNotFoundError: 404,
    NoPairAvailableError: 409,
}


def _status_for(error: TagRankError) -> int:
    for error_type, status in _ERROR_STATUS.items():
        if isinstance(error, error_type):
            return status
    return 502  # default: something about the Hydrus connection/data went wrong


def _raise_http(error: TagRankError) -> None:
    raise HTTPException(status_code=_status_for(error), detail={"error": type(error).__name__, "message": str(error)})


# --------------------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------------------

class StartSessionRequest(BaseModel):
    query: list[str] | None = Field(
        default=None,
        description="Hydrus search predicates for the comparison pool, e.g. ['character:mario']. "
                    "Omit to use the SEARCH_QUERY setting. Use the tag string from GET /search-options "
                    "to search around a specific rated tag.",
    )
    pool_size: int | None = Field(default=None, description="Override the configured POOL_SIZE for this session.")


class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "ready", "error"]
    session_id: str | None = None
    error: str | None = None


class FilePairSide(BaseModel):
    file_id: int
    hash: str


class PairResponse(BaseModel):
    left: FilePairSide | None
    right: FilePairSide | None
    done: bool = Field(description="True if no pair is available (pool exhausted).")


class SubmitResultRequest(BaseModel):
    choice: Literal["left", "right"] = Field(description="Which side of the last pair returned by GET .../next-pair won.")


class TagInfo(BaseModel):
    tag: str
    score: float


class TagOptionOut(BaseModel):
    index: int
    tag: str
    score: float
    file_count: int


class SearchOptionsResponse(BaseModel):
    top: list[TagOptionOut]
    random: list[TagOptionOut]
    bottom: list[TagOptionOut]


class GraphInfo(BaseModel):
    title: str
    png_base64: str


# --------------------------------------------------------------------------------------
# Health / lifecycle
# --------------------------------------------------------------------------------------

@app.get("/health", summary="Liveness check")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/shutdown", summary="Gracefully stop the server")
async def shutdown() -> dict[str, str]:
    """Prefer this over killing the process, so any in-memory session state has a chance to be
    persisted via DELETE /sessions/{id} first. Undertow should call that for its active session
    before calling this."""
    async def _stop():
        await asyncio.sleep(0.1)
        import os
        import signal
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_stop())
    return {"status": "shutting down"}


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------

_jobs: dict[str, JobStatus] = {}


def _run_start_session(job_id: str, query: list[str] | None, pool_size: int | None) -> None:
    try:
        session = service.start_session(query=query, pool_size=pool_size)
        _jobs[job_id] = JobStatus(job_id=job_id, status="ready", session_id=session.id)
    except TagRankError as e:
        _jobs[job_id] = JobStatus(job_id=job_id, status="error", error=str(e))


@app.post(
    "/sessions",
    response_model=JobStatus,
    summary="Start a rating-comparison session (async)",
    description="Builds a comparison pool from Hydrus (can take a few seconds to tens of "
                "seconds) and starts a RatingSystem. Returns immediately with a job id; poll "
                "GET /sessions/{job_id} until status is 'ready', then use the returned session_id "
                "with the /sessions/{id}/... routes below.",
)
async def create_session(request: StartSessionRequest) -> JobStatus:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobStatus(job_id=job_id, status="pending")
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_start_session, job_id, request.query, request.pool_size)
    return _jobs[job_id]


@app.get("/sessions/{job_id}", response_model=JobStatus, summary="Poll a session-start job")
def get_job_status(job_id: str) -> JobStatus:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "JobNotFoundError", "message": f"No job with id '{job_id}'"})
    return job


def _get_session_or_404(session_id: str) -> Session:
    try:
        return service.get_session(session_id)
    except TagRankError as e:
        _raise_http(e)
        raise  # unreachable, satisfies type checkers


def _pair_to_response(pair) -> PairResponse:
    if pair is None:
        return PairResponse(left=None, right=None, done=True)
    left, right = pair
    return PairResponse(
        left=FilePairSide(file_id=left["file_id"], hash=left["hash"]),
        right=FilePairSide(file_id=right["file_id"], hash=right["hash"]),
        done=False,
    )


@app.get(
    "/sessions/{session_id}/next-pair",
    response_model=PairResponse,
    summary="Get the next file pair to compare",
)
def next_pair(session_id: str) -> PairResponse:
    session = _get_session_or_404(session_id)
    pair = service.get_next_pair(session)
    return _pair_to_response(pair)


@app.post(
    "/sessions/{session_id}/result",
    summary="Submit the winner of the last pair",
    description="Judges the pair most recently returned by GET .../next-pair. Call "
                "GET .../next-pair again afterwards to continue the session.",
)
def submit_result(session_id: str, request: SubmitResultRequest) -> dict[str, str]:
    session = _get_session_or_404(session_id)
    try:
        service.submit_result(session, request.choice)
    except TagRankError as e:
        _raise_http(e)
    return {"status": "ok"}


@app.post("/sessions/{session_id}/undo", summary="Undo the last submitted result")
def undo(session_id: str) -> dict[str, str]:
    session = _get_session_or_404(session_id)
    service.undo(session)
    return {"status": "ok"}


@app.delete(
    "/sessions/{session_id}",
    summary="End a session",
    description="Persists ratings/comparisons to disk (same as closing the GUI window) and "
                "discards the in-memory session. Always call this when Undertow is done with "
                "a session, even if the user cancelled - otherwise the comparisons made during "
                "it are lost.",
)
def end_session(session_id: str) -> dict[str, str]:
    session = _get_session_or_404(session_id)
    service.end_session(session)
    return {"status": "ended"}


# --------------------------------------------------------------------------------------
# Tag / history / graph data
# --------------------------------------------------------------------------------------

@app.get("/tags", response_model=list[TagInfo], summary="List all rated tags")
def list_tags() -> list[TagInfo]:
    return [TagInfo(**entry) for entry in service.list_tags()]


@app.get(
    "/search-options",
    response_model=SearchOptionsResponse,
    summary="Get the Top/Random/Bottom tag picker options",
    description="Same categories the CLI offers at startup for choosing a comparison pool's "
                "starting tag. Use a returned tag string as the `query` for POST /sessions.",
)
def search_options() -> SearchOptionsResponse:
    try:
        options = service.get_search_options()
    except TagRankError as e:
        _raise_http(e)
        raise
    return SearchOptionsResponse(
        top=[TagOptionOut(**o._asdict()) for o in options.top],
        random=[TagOptionOut(**o._asdict()) for o in options.random],
        bottom=[TagOptionOut(**o._asdict()) for o in options.bottom],
    )


@app.get(
    "/history/predictions",
    summary="Raw prediction-log history",
    description="The per-comparison records the summary charts are built from - one entry per "
                "judged pair, including which model predicted correctly and the confidence score. "
                "Useful for building custom visualizations Undertow-side.",
)
def prediction_history() -> list[dict]:
    return service.get_prediction_history()


@app.get(
    "/history/graphs",
    response_model=list[GraphInfo],
    summary="Session summary charts as base64 PNGs",
    description="The same four charts tagrank's Summary Dashboard shows, each as a base64-encoded "
                "PNG suitable for a data-URI <img> tag. For a directly embeddable image URL instead, "
                "use GET /history/graphs/{index}.png.",
)
def history_graphs() -> list[GraphInfo]:
    return [GraphInfo(**g) for g in service.get_session_graphs()]


@app.get(
    "/history/graphs/{index}.png",
    summary="One session summary chart as a raw PNG",
    description="Same charts as GET /history/graphs, but returns a single chart's raw image "
                "bytes so it can be used directly as an <img src> without decoding base64.",
)
def history_graph_png(index: int) -> Response:
    graphs_list = service.get_session_graphs()
    if not (0 <= index < len(graphs_list)):
        raise HTTPException(status_code=404, detail={"error": "GraphNotFoundError", "message": f"No graph at index {index}"})
    import base64
    return Response(content=base64.b64decode(graphs_list[index]["png_base64"]), media_type="image/png")
