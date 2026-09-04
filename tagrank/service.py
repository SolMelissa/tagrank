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

from tagrank import badges, graphs, pool, presets, tournament as tournament_module
from tagrank.errors import FileInformationError, NoRelevantFilesError, TagRankError
from tagrank.hydrus_client import create_client
from tagrank.rating import (
    FileMetaData,
    RatingSystem,
    load_current_tag_ratings,
    trueskill_confidence_from_rating,
    trueskill_number_from_rating,
)
from tagrank.settings import Settings, get_settings_store, load_settings


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
    tournament: tournament_module.Tournament | None = field(default=None, repr=False)
    tournament_seed_tag: str | None = field(default=None, repr=False)


_sessions: dict[str, Session] = {}


def start_session(
    query: list[str] | None = None,
    pool_size: int | None = None,
    settings: Settings | None = None,
    client: hydrus_api.Client | None = None,
    preset_id: str | None = None,
    pool_strategy: str | None = None,
    use_similarity: bool = True,
) -> Session:
    """Build a comparison pool and RatingSystem, exactly like app.run_for_rank_tags does before
    it opens the GUI window. Raises NoRelevantFilesError / FileInformationError on failure.

    `preset_id` (Mood/Theme Sessions) fills in query/pool_size/pool_strategy from
    config/presets.json for anything the caller didn't explicitly override. `pool_strategy`
    (Top/Random/Bottom/Confidence Duel/Divergence) is applied to the session's settings so
    RatingSystem.get_file_pair() picks pairs accordingly.
    """
    settings = settings or get_settings_store().current
    if preset_id is not None:
        preset = presets.get_preset(preset_id)
        if preset is not None:
            query = query if query is not None else preset.query
            pool_size = pool_size if pool_size is not None else preset.pool_size
            pool_strategy = pool_strategy if pool_strategy is not None else preset.pool_strategy
            if preset.max_distance_start is not None or preset.max_distance_hard is not None:
                from dataclasses import replace
                settings = replace(settings, distance=replace(
                    settings.distance,
                    max_distance_start=preset.max_distance_start or settings.distance.max_distance_start,
                    max_distance_hard=preset.max_distance_hard or settings.distance.max_distance_hard,
                ))
    if pool_strategy is not None:
        from dataclasses import replace
        settings = replace(settings, pool=replace(settings.pool, pool_strategy=pool_strategy))

    client = client or create_client(settings)

    file_service_key = settings.pool.file_service_key
    hashes = (
        pool.build_pool(client=client, query=query, pool_size=pool_size, file_service_key=file_service_key, use_similarity=use_similarity)
        if pool_size
        else pool.build_pool(client=client, query=query, file_service_key=file_service_key, use_similarity=use_similarity)
    )
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


def get_settings() -> Settings:
    """Current effective settings, shared by the GUI panel and GET /settings."""
    return get_settings_store().current


def patch_settings(changes: dict[str, object]) -> Settings:
    """Apply a partial settings edit (see settings.SettingsStore) and return the new
    effective Settings, shared by the GUI panel and PATCH /settings."""
    return get_settings_store().update(changes)


def get_presets() -> list[presets.Preset]:
    return presets.load_presets()


def get_badges() -> dict[str, dict[str, list[dict]]]:
    """All earned badges, keyed by entity type then tag/file-hash. See tagrank/badges.py."""
    return badges.load_badges()


def _badge_out(badge_id: str | None) -> dict[str, Any] | None:
    if badge_id is None:
        return None
    badge = badges.BADGE_BY_ID.get(badge_id)
    if badge is None:
        return None
    return {"id": badge.id, "name": badge.name, "icon": badge.icon, "difficulty": badge.difficulty}


def _all_badges_out(entity_type: badges.EntityType, entity_id: str) -> list[dict[str, Any]]:
    """Every badge an entity currently holds (not just the rarest one), highest difficulty
    first then alphabetically by name - for UI surfaces that want to show/list them all
    (image corner overlays, tag hover tooltips) rather than just a single rarity callout."""
    held = badges.held_badge_ids(entity_type, entity_id)
    out = [_badge_out(bid) for bid in held]
    out = [b for b in out if b is not None]
    out.sort(key=lambda b: (-badges.DIFFICULTY_ORDER.get(b["difficulty"], 0), b["name"]))
    return out


def get_rating_details(file_id: int, file_hash: str, tags: list[str], settings: Settings | None = None) -> dict[str, Any]:
    """One picture's TrueSkill score/rarest badge plus per-tag score/badge_count, for
    Undertow's embedded comparer (see plans/undertow-comparer-rating-details.md - the contract
    this implements). Picture ratings only live in Hydrus (as two plain numeric rating-service
    values TagRank itself writes post-comparison - see RatingSystem.write_file_mmr_rating/
    write_file_mmr_confidence_rating), so unlike tag scores there's no local on-disk store to
    read them back from; a fresh Hydrus metadata fetch by hash is unavoidable here. Reading
    those two numbers straight off the fetched metadata (rather than round-tripping them through
    a freshly-bootstrapped Rating(), which RatingSystem.file_rating_for_file always seeds with
    the *default* sigma and would make photo_confidence come back as 0% for every file that
    hasn't already been re-compared in *this* process's lifetime) is what actually reproduces
    the number the user last saw written, not a degenerate reconstruction of it.
    """
    settings = settings or load_settings()
    photo_score: float | None = None
    photo_confidence: float | None = None
    picture_badge = _badge_out(badges.rarest_badge_id("picture", file_hash))
    picture_badges = _all_badges_out("picture", file_hash)

    try:
        client = create_client(settings)
        metadata_response = client.get_file_metadata(hashes=[file_hash])
        entries = (metadata_response or {}).get("metadata") or []
        ratings = (entries[0].get("ratings") or {}) if entries else {}
    except Exception:
        ratings = {}

    mmr_key = settings.hydrus.mmr_service_key
    confidence_key = settings.hydrus.mmr_confidence_service_key
    if mmr_key and mmr_key in ratings:
        try:
            photo_score = float(ratings[mmr_key])
        except (TypeError, ValueError):
            photo_score = None
    if confidence_key and confidence_key in ratings:
        try:
            photo_confidence = float(ratings[confidence_key])
        except (TypeError, ValueError):
            photo_confidence = None

    tag_ratings = load_current_tag_ratings()
    tags_out = []
    for tag in tags:
        rating = tag_ratings.get(tag)
        tag_badges = _all_badges_out("tag", tag)
        tags_out.append({
            "tag": tag,
            "score": trueskill_number_from_rating(rating) if rating is not None else None,
            "confidence": trueskill_confidence_from_rating(rating) if rating is not None else None,
            "badge_count": len(tag_badges),
            "badges": tag_badges,
        })

    tag_scores = [t["score"] for t in tags_out if t["score"] is not None]
    avg_tag_score = sum(tag_scores) / len(tag_scores) if tag_scores else None
    if photo_score is not None and avg_tag_score is not None:
        total_score = photo_score + avg_tag_score
    elif photo_score is not None:
        total_score = photo_score
    else:
        total_score = avg_tag_score

    return {
        "photo_score": photo_score,
        "photo_confidence": photo_confidence,
        "picture_badge": picture_badge,
        "picture_badges": picture_badges,
        "avg_tag_score": avg_tag_score,
        "total_score": total_score,
        "tags": tags_out,
    }


def start_tournament(session: Session) -> tournament_module.Tournament:
    """Draw a random bracket from the session's pool and attach it to the session."""
    settings = session.rating_system.settings
    pool_ids = session.rating_system.file_ids
    tournament = tournament_module.start_tournament(pool_ids, settings.pool.max_tournament_size)
    session.tournament = tournament
    return tournament


def get_tournament_pair(session: Session) -> tuple[FileMetaData, FileMetaData] | None:
    """Next pending tournament match as a metadata pair, or None if the bracket is complete."""
    if session.tournament is None:
        raise NoPairAvailableError("No tournament is active on this session; call start_tournament first.")
    match = session.tournament.pending_match()
    if match is None:
        return None
    pair = session.rating_system.convert_image_ids_to_file_meta_data((match.left_id, match.right_id))
    if pair is not None:
        session.left, session.right = pair
    return pair


def submit_tournament_result(session: Session, choice: str) -> None:
    """Judge the current tournament match: updates ratings exactly like submit_result(), then
    advances the bracket and awards tournament badges once a champion is crowned."""
    if session.tournament is None or session.left is None or session.right is None:
        raise NoPairAvailableError("No tournament match is pending judgement.")
    match = session.tournament.pending_match()
    if match is None:
        raise NoPairAvailableError("The tournament bracket is already complete.")

    left, right = session.left, session.right
    winner, loser = (left, right) if choice == "left" else (right, left)
    winner_id, loser_id = int(winner["file_id"]), int(loser["file_id"])

    session.tournament.bracket_id_to_hash[winner_id] = winner["hash"]
    session.tournament.bracket_id_to_hash[loser_id] = loser["hash"]

    submit_result(session, choice)  # normal rating update + regular badges/underdog alert

    mu_by_id = {fid: r.mu for fid, r in session.rating_system.file_ratings.items()}
    sigma_by_id = {fid: r.sigma for fid, r in session.rating_system.file_ratings.items()}
    tournament_module.check_bracket_buster(session.tournament, match, winner_id, mu_by_id, sigma_by_id)
    session.tournament.record_winner(match, winner_id)

    if session.tournament.is_complete:
        tournament_module.finish_tournament(session.tournament, seed_tag=session.tournament_seed_tag)


def pick_rediscovery_pair(session: Session) -> tuple[FileMetaData, FileMetaData] | None:
    """Random Rediscovery: pair a highly-rated, rarely-recently-seen file against a random
    opponent from the pool. The rediscovered file is flagged so a win credits its
    rediscovery-specific badges (see RatingSystem.inject_rediscovery_pick)."""
    rediscovered_id = session.rating_system.pick_rediscovery_file_id()
    if rediscovered_id is None or len(session.rating_system.file_ids) < 2:
        return None
    import random as _random
    opponent_id = _random.choice([fid for fid in session.rating_system.file_ids if fid != rediscovered_id])
    session.rating_system.inject_rediscovery_pick(rediscovered_id)
    pair = session.rating_system.convert_image_ids_to_file_meta_data((rediscovered_id, opponent_id))
    if pair is not None:
        session.left, session.right = pair
    return pair


def get_search_options(client: hydrus_api.Client | None = None, settings: Settings | None = None) -> pool.SearchOptions:
    """The same Top/Random/Bottom tag categories the CLI offers at startup (pool.prompt_for_search)."""
    settings = settings or load_settings()
    client = client or create_client(settings)
    return pool.build_search_options(client, file_service_key=settings.pool.file_service_key)


def get_filtered_search_options(
    filters: pool.FilterParams, client: hydrus_api.Client | None = None, settings: Settings | None = None,
) -> pool.SearchOptions:
    """DB Search variant of get_search_options: narrows the Top/Random/Bottom tag picker by
    every filter axis (score/resolution/rating-count/date/namespace/archive/service-key),
    computed from the cached tag_index rather than a fresh Hydrus search per candidate tag.
    Used by POST /search-options/filtered."""
    settings = settings or load_settings()
    client = client or create_client(settings)
    return pool.build_filtered_search_options(client, filters)


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
    try:
        for title, figure in zip(titles, figures):
            buf = io.BytesIO()
            figure.savefig(buf, format="png", dpi=110)
            results.append({"title": title, "png_base64": base64.b64encode(buf.getvalue()).decode("ascii")})
    finally:
        # Each call to build_session_summary_figures() registers new Figure objects with
        # pyplot's global figure manager; unlike summary_dashboard.py (which closes the old
        # figure on every refresh), nothing here ever reclaims them. Left unclosed, repeated
        # GET /history/graphs calls against a long-running --serve process leak memory
        # without bound.
        import matplotlib.pyplot as plt  # local import: keep Agg backend selection above authoritative
        for figure in figures:
            plt.close(figure)
    return results
