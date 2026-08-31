"""Headless business-logic layer used by tagrank/server.py.

Every function here wraps existing, already-tested logic (RatingSystem in tagrank/rating.py,
pool assembly in tagrank/pool.py, chart building in tagrank/graphs.py) with no Qt, no print(),
and no sys.exit — failures raise a tagrank.errors.TagRankError subclass so the HTTP layer can
turn them into structured error responses. This mirrors exactly what tagrank/ui/window.py's
keyPressEvent already does for a single comparison (write_prediction_log_entry -> process_result
-> pool.write_choice -> get_file_pair), just triggered by an API call instead of a keypress.
"""

import base64
import io
import uuid
from dataclasses import dataclass, field
from typing import Any

import hydrus_api  # type: ignore

# Must run before `tagrank.graphs` (and matplotlib.pyplot) is imported by anything else in this
# process. The API renders charts to PNG bytes from a FastAPI worker thread, not the main thread,
# and matplotlib's default interactive backend segfaults when driven off-main-thread; Agg is the
# non-interactive, thread-safe rendering backend. Safe process-wide here because --serve mode
# never opens the Qt GUI (tagrank/ui/), which is the only thing that needs an interactive backend.
import matplotlib
matplotlib.use("Agg")

from tagrank import graphs, pool
from tagrank.errors import FileInformationError, NoRelevantFilesError, TagRankError
from tagrank.hydrus_client import create_client
from tagrank.rating import FileMetaData, RatingSystem
from tagrank.settings import Settings, load_settings


class SessionNotFoundError(TagRankError):
    """No active session exists with the given id (never started, or already ended)."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"No active session with id '{session_id}'")


class NoPairAvailableError(TagRankError):
    """submit_result was called before get_next_pair produced a pair to judge."""


@dataclass
class Session:
    """One in-memory rating session: a RatingSystem plus the pair most recently handed out,
    so submit_result() knows what 'left'/'right' refers to without the caller re-sending it."""
    id: str
    rating_system: RatingSystem
    client: hydrus_api.Client
    left: FileMetaData | None = field(default=None, repr=False)
    right: FileMetaData | None = field(default=None, repr=False)


_sessions: dict[str, Session] = {}


def start_session(
    query: list[str] | None = None,
    pool_size: int | None = None,
    settings: Settings | None = None,
    client: hydrus_api.Client | None = None,
) -> Session:
    """Build a comparison pool and RatingSystem, exactly like app.run_for_rank_tags does before
    it opens the GUI window. Raises NoRelevantFilesError / FileInformationError on failure."""
    settings = settings or load_settings()
    client = client or create_client(settings)

    hashes = pool.build_pool(client=client, query=query, pool_size=pool_size) if pool_size else pool.build_pool(client=client, query=query)
    if not hashes:
        raise NoRelevantFilesError(query or [])

    metadata_response = client.get_file_metadata(hashes=hashes)
    if metadata_response is None or metadata_response.get("metadata") is None:
        raise FileInformationError("Was not able to fetch file information for the pool.")

    ids = [int(meta["file_id"]) for meta in metadata_response["metadata"] if "file_id" in meta]
    if len(ids) < 2:
        raise NoRelevantFilesError(query or [])

    rating_system = RatingSystem(client, ids, settings)
    session = Session(id=str(uuid.uuid4()), rating_system=rating_system, client=client)
    _sessions[session.id] = session
    return session


def get_session(session_id: str) -> Session:
    try:
        return _sessions[session_id]
    except KeyError:
        raise SessionNotFoundError(session_id)


def get_next_pair(session: Session) -> tuple[FileMetaData, FileMetaData] | None:
    """Wraps RatingSystem.get_file_pair(); caches the pair on the session for submit_result()."""
    pair = session.rating_system.get_file_pair()
    session.left, session.right = pair if pair is not None else (None, None)
    return pair


def submit_result(session: Session, choice: str) -> None:
    """Record a comparison result for the pair returned by the last get_next_pair() call.

    `choice` is "left" or "right", picking which of that pair won. Mirrors
    tagrank/ui/window.py's keyPressEvent: logs the prediction, updates TrueSkill ratings,
    and writes the like/dislike choice back to Hydrus for both files.
    """
    if session.left is None or session.right is None:
        raise NoPairAvailableError("No pair is pending judgement; call get_next_pair first.")
    if choice not in ("left", "right"):
        raise ValueError(f"choice must be 'left' or 'right', got {choice!r}")

    left, right = session.left, session.right
    winner, loser = (left, right) if choice == "left" else (right, left)
    label = "A" if choice == "left" else "B"

    session.rating_system.write_prediction_log_entry(left, right, label)
    session.rating_system.process_result(winner=winner, loser=loser)
    pool.write_choice(winner["hash"], liked=True, client=session.client)
    pool.write_choice(loser["hash"], liked=False, client=session.client)
    session.left, session.right = None, None


def undo(session: Session) -> None:
    session.rating_system.process_undo()


def end_session(session: Session) -> None:
    """Persist ratings/comparisons to disk (as the GUI does on window close) and drop the session."""
    session.rating_system.write_results_to_file()
    _sessions.pop(session.id, None)


def list_tags() -> list[dict[str, Any]]:
    """All rated tags with their TrueSkill score, sorted strongest first."""
    ratings = pool.load_ratings()
    return sorted(
        ({"tag": tag, "score": pool.trueskill_score(rating)} for tag, rating in ratings.items()),
        key=lambda entry: entry["score"],
        reverse=True,
    )


def get_search_options(client: hydrus_api.Client | None = None, settings: Settings | None = None) -> pool.SearchOptions:
    """The same Top/Random/Bottom tag categories the CLI offers at startup (pool.prompt_for_search)."""
    client = client or create_client(settings or load_settings())
    return pool.build_search_options(client)


def get_prediction_history() -> list[dict[str, Any]]:
    """Raw per-comparison prediction-tracking records that graphs.build_session_summary_figures
    charts — exposed as-is so a caller can build its own visualizations from the same data."""
    return graphs.load_prediction_entries()


def get_session_graphs(
    amount_of_tags: int = 20,
    figure_height: int = 700,
) -> list[dict[str, Any]]:
    """Render the same summary charts tagrank/ui/summary_dashboard.py shows, as embeddable PNGs.

    Returns a list of {"title": str, "png_base64": str} in the same order the dashboard shows
    them: rolling prediction accuracy, ratings per date, confidence calibration, final tag
    rankings. No session/client needed - this only reads already-persisted ratings.json and
    prediction_log.jsonl, so it also works when no rating session is active.
    """
    prediction_entries = graphs.load_prediction_entries()
    # RatingSystem's constructor loads current_ratings from disk without touching the client,
    # so a throwaway instance is enough to compute top tags for the rankings chart.
    rating_system = RatingSystem(client=None, file_ids=[])  # type: ignore[arg-type]
    top_tags = graphs.top_tags_from_rating_system(rating_system, amount_of_tags)

    figures = graphs.build_session_summary_figures(prediction_entries, top_tags, figure_height=figure_height)
    titles = [
        "Rolling Prediction Accuracy Over Time",
        "Ratings per Session Date",
        "Tag Model Confidence Calibration",
        "Final Tag Rankings by TrueSkill Score",
    ]
    results = []
    for title, figure in zip(titles, figures):
        buf = io.BytesIO()
        figure.savefig(buf, format="png", dpi=110)
        results.append({"title": title, "png_base64": base64.b64encode(buf.getvalue()).decode("ascii")})
    return results
