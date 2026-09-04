#!/usr/bin/env python3
"""tagrank_pool.py

Builds a pool of ~POOL_SIZE visually-similar files from a Hydrus client to feed
into TagRank (main.py).

Search selection
----------------
The pool's "start point" is chosen interactively via prompt_for_search():
it reads ratings.json, ranks every tag by its TrueSkill score (mu - 3*sigma),
and offers the top tags as numbered options. Selecting one uses it as the
search predicates; 0 opens a custom (comma-separated) search. This decouples
the dataset from the SEARCH_QUERY setting, so editing that setting no longer
silently swaps the pool.

Pool assembly
-------------
Similarity search uses hydrus's perceptual-hash engine via the system predicate
`system:similar to <hash> distance <n>`. Multiple such predicates fold into ONE
/search_files call capped with `system:limit = pool_size * fuzz`, so hydrus
returns ~pool_size similar files in a single round-trip (no 40x seed re-
queries).
"""

import json
import logging
import random
import time
from typing import Any, NamedTuple

import hydrus_api

from config import (
    DATA_DIR,
    ensure_config_files,
    get,
    get_int,
    get_bool,
    get_float_or_none,
    get_list,
    key,
    is_excluded_tag,
)
from tagrank.errors import UnknownServiceKeyError
from tagrank.tag_index import FileRecord, TagIndex
from tagrank.tag_utils import has_namespace

# --- pull settings out of config/KEYS and config/SETTINGS ---
API_URL            = key("API_URL", "http://127.0.0.1:45869/")
API_KEY            = key("API_KEY")
RATING_SERVICE_KEY = key("RATING_SERVICE_KEY")

POOL_SIZE            = get_int("POOL_SIZE", 100)
MAX_DISTANCE_START  = get_int("MAX_DISTANCE_START", 10)
DISTANCE_STEP       = get_int("DISTANCE_STEP", 2)
MAX_DISTANCE_HARD   = get_int("MAX_DISTANCE_HARD", 64)
MIN_POOL_SATISFIED  = get_float_or_none("MIN_POOL_SATISFIED", None)
CANDIDATE_SEED_COUNT = get_int("CANDIDATE_SEED_COUNT", 10000)
SEED_COUNT_FOR_QUERY = get_int("SEED_COUNT_FOR_QUERY", 10)
API_LIMIT_FUZZ       = get_int("API_LIMIT_FUZZ", 2)
FILE_SERVICE_KEY     = get("FILE_SERVICE_KEY", "").strip()
if FILE_SERVICE_KEY == "FILL_ME_IN":
    FILE_SERVICE_KEY = ""
TOP_TAG_OPTIONS      = get_int("TOP_TAG_OPTIONS", 20)
BOTTOM_TAG_OPTIONS   = get_int("BOTTOM_TAG_OPTIONS", 10)
MIN_TAG_FILE_COUNT   = get_int("MIN_TAG_FILE_COUNT", 1)
RANDOM_TAG_OPTIONS   = get_int("RANDOM_TAG_OPTIONS", 10)
DEBUG_MODE           = get_bool("DEBUG_MODE", True)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

_DEFAULT_CLIENT = None


def _get_default_client() -> hydrus_api.Client:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = hydrus_api.Client(API_KEY, API_URL)
    return _DEFAULT_CLIENT


# ------------------------- rating-based search selection -------------------------

def load_ratings() -> dict[str, tuple[float, float]]:
    """Load {tag: (mu, sigma)} from ratings.json (TrueSkill params) and reconcile with
    Hydrus's current sibling map (migrate ratings keyed by a tag's old non-ideal name
    to its current ideal tag, per the sibling handling carve-out)."""
    try:
        with open(DATA_DIR / "ratings.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load ratings.json: {e}")
        return {}
    ratings: dict[str, tuple[float, float]] = {}
    for entry in data:
        try:
            tag, (mu, sigma) = entry
            tag_str = str(tag)
            if not is_excluded_tag(tag_str):
                ratings[tag_str] = (float(mu), float(sigma))
        except (ValueError, TypeError):
            continue

    # One-time reconciliation: migrate ratings from sibling-collapsed non-ideal tags to their
    # current ideal tags. Unnamespaced tags only — namespaced tags survive as-is per the rule.
    try:
        client = _get_default_client()
        unnamespaced_tags = [tag for tag in ratings.keys() if not has_namespace(tag)]
        if unnamespaced_tags:
            siblings_response = client.get_siblings_and_parents(unnamespaced_tags)
            ideal_map = siblings_response.get("ideal_tags", {}) if siblings_response else {}
            to_merge: dict[str, list[tuple[str, tuple[float, float]]]] = {}  # ideal -> [(old_tag, rating), ...]

            for tag, ideal_tag in ideal_map.items():
                if tag != ideal_tag and tag in ratings:
                    # Tag's ideal has changed - this rating should migrate to the ideal
                    old_rating = ratings.pop(tag)
                    if ideal_tag not in to_merge:
                        to_merge[ideal_tag] = []
                    to_merge[ideal_tag].append((tag, old_rating))

            for ideal_tag, old_ratings_list in to_merge.items():
                # Pick the highest-confidence (lowest sigma) old rating to merge
                best_old_tag, best_old_rating = min(old_ratings_list, key=lambda x: x[1][1])

                if ideal_tag in ratings:
                    # Ideal tag already has a rating — keep the higher-confidence one
                    old_mu, old_sigma = best_old_rating
                    current_mu, current_sigma = ratings[ideal_tag]
                    if old_sigma < current_sigma:
                        ratings[ideal_tag] = best_old_rating
                        logger.info(f"Merged {best_old_tag} into {ideal_tag}, favoring migrated (sigma={old_sigma:.2f})")
                    else:
                        logger.info(f"Merged {best_old_tag} into {ideal_tag}, keeping existing (higher confidence)")
                else:
                    ratings[ideal_tag] = best_old_rating
                    logger.info(f"Migrated {best_old_tag} → {ideal_tag} (sibling reconciliation)")
    except Exception as e:
        logger.warning(f"Sibling reconciliation failed (ratings still usable): {e}")

    return ratings


def trueskill_score(rating: tuple[float, float]) -> float:
    """scoring used across TagRank: mu - 3*sigma."""
    mu, sigma = rating
    return mu - 3 * sigma


def get_tag_file_count(tag: str, client: hydrus_api.Client, file_service_key: str = "") -> int:
    """Number of files in the Hydrus db currently carrying `tag`."""
    try:
        kwargs = {"file_service_keys": [file_service_key]} if file_service_key else {}
        resp = client.search_files([tag], return_hashes=True, **kwargs)
        return len(resp.get("hashes") or [])
    except Exception as e:
        logger.error(f"Could not get file count for tag '{tag}': {e}")
        return 0


class TagOption(NamedTuple):
    """One selectable tag in the search picker, as shown to a user or an API caller."""
    index: int
    tag: str
    score: float
    file_count: int


class SearchOptions(NamedTuple):
    """The top/random/bottom-scoring tag categories offered as search starting points.

    `index` on each TagOption is the number a CLI user would type to pick it (also usable
    by an API caller as an opaque id); look it up via `lookup[index]` to get the tag string.
    """
    top: list[TagOption]
    random: list[TagOption]
    bottom: list[TagOption]
    lookup: dict[int, str]


def _bucket_top_random_bottom(entries: list[tuple[str, float, int]]) -> SearchOptions:
    """Shared Top/Random/Bottom tag-selection logic: given (tag, trueskill_score, file_count)
    triples (already filtered/scored by the caller), rank by score and slice into the three
    categories the CLI prompt and the /search-options* API endpoints both offer. Factored out
    so build_search_options (unfiltered) and build_filtered_search_options (DB Search) share
    one selection implementation instead of duplicating it."""
    ranked = sorted(entries, key=lambda e: e[1])
    all_tags = [tag for tag, _score, _count in ranked]
    score_by_tag = {tag: score for tag, score, _count in ranked}
    count_by_tag = {tag: count for tag, _score, count in ranked}

    top_tags = all_tags[-TOP_TAG_OPTIONS:] if TOP_TAG_OPTIONS > 0 else []
    bottom_tags = all_tags[:BOTTOM_TAG_OPTIONS] if BOTTOM_TAG_OPTIONS > 0 else []

    # Strongest first for Top, lowest first for Bottom
    top_tags = list(reversed(top_tags))

    excluded = set(top_tags) | set(bottom_tags)
    remaining = [tag for tag in all_tags if tag not in excluded]
    random_tags = (
        random.sample(remaining, k=min(RANDOM_TAG_OPTIONS, len(remaining)))
        if RANDOM_TAG_OPTIONS > 0
        else []
    )

    def _to_options(tags: list[str], start_index: int) -> list[TagOption]:
        return [
            TagOption(index=idx, tag=tag, score=score_by_tag[tag], file_count=count_by_tag[tag])
            for idx, tag in enumerate(tags, start=start_index)
        ]

    top_options = _to_options(top_tags, 1)
    random_options = _to_options(random_tags, 1 + len(top_options))
    bottom_options = _to_options(bottom_tags, 1 + len(top_options) + len(random_options))

    lookup = {opt.index: opt.tag for opt in top_options + random_options + bottom_options}

    return SearchOptions(top=top_options, random=random_options, bottom=bottom_options, lookup=lookup)


def build_search_options(client: hydrus_api.Client | None = None, file_service_key: str = "") -> SearchOptions:
    """Pure data half of the search picker: ranks tags by TrueSkill score and buckets them
    into Top/Random/Bottom categories, same selection logic the CLI prompt renders. Used
    directly by the API's /search-options endpoint, and by prompt_for_search() below.

    Computed from the in-memory tag_index (see tagrank/tag_index.py) rather than one live
    Hydrus file-count round trip per rated tag - that per-request cost is what used to force
    this endpoint's 180s timeout on a real library."""
    client = client or _get_default_client()
    from tagrank.tag_index import ensure_index  # local import: avoid a cycle
    index = ensure_index(client)
    return build_search_options_from_index(index, file_service_key)


def build_search_options_from_index(index: TagIndex, file_service_key: str = "") -> SearchOptions:
    ratings = load_ratings()
    entries: list[tuple[str, float, int]] = []
    for tag, file_ids in index.tag_to_file_ids.items():
        if tag not in ratings or tag.startswith("filename:") or is_filtered_tag(tag):
            continue
        if file_service_key:
            count = sum(
                1 for fid in file_ids
                if (record := index.files.get(fid)) is not None and file_service_key in record.file_service_keys
            )
        else:
            count = len(file_ids)
        if count < MIN_TAG_FILE_COUNT:
            continue
        entries.append((tag, trueskill_score(ratings[tag]), count))
    return _bucket_top_random_bottom(entries)


class FilterParams(NamedTuple):
    """POST /search-options/filtered request, already resolved to absolute min/max values by
    the caller (Undertow's DB Search filter bar) - see plans/undertow-filtered-search-api.md."""
    filter_tag: str = ""
    min_files: int = 0
    score_min: float = float("-inf")
    score_max: float = float("inf")
    aspect_ratio_min: float = 0.0
    aspect_ratio_max: float = float("inf")
    pixel_count_min: float = 0.0
    pixel_count_max: float = float("inf")
    rating_count_min: float = 0.0
    rating_count_max: float = float("inf")
    date_added_days_ago_min: float = 0.0
    date_added_days_ago_max: float | None = None
    namespace_mode: str = "all"
    archive_mode: str = "all"
    file_service_keys: list[str] | None = None
    tag_service_keys: list[str] | None = None


def _validate_service_keys(client: hydrus_api.Client, filters: FilterParams) -> None:
    """Raise UnknownServiceKeyError if any explicitly-requested service key isn't one Hydrus
    currently reports via GET /get_services. Empty/None means 'all services' and is not checked."""
    requested = list(filters.file_service_keys or []) + list(filters.tag_service_keys or [])
    if not requested:
        return
    services_response = client.get_services()
    known_keys = set(services_response.get("services", {}).keys())
    for service_key in requested:
        if service_key not in known_keys:
            raise UnknownServiceKeyError(service_key)


def build_filtered_search_options(client: hydrus_api.Client, filters: FilterParams) -> SearchOptions:
    """DB Search variant of build_search_options: same TrueSkill-ranked Top/Random/Bottom
    tag picker, but each candidate tag is first narrowed by every filter axis in `filters`.
    Computed entirely from the in-memory tag_index (see tagrank/tag_index.py) rather than a
    fresh Hydrus round trip per candidate tag - that per-request live-query design was slow
    enough on a real library to make Undertow's DB Search button "just spin". An empty result
    after filtering is a normal empty SearchOptions, not an error - only a genuinely broken
    input (unknown service key) raises."""
    _validate_service_keys(client, filters)
    from tagrank.tag_index import ensure_index  # local import: avoid a cycle (tag_index imports us)
    index = ensure_index(client)
    return build_filtered_search_options_from_index(index, filters)


def build_filtered_search_options_from_index(index: TagIndex, filters: FilterParams) -> SearchOptions:
    ratings = load_ratings()
    filter_tag_lower = filters.filter_tag.strip().lower()

    entries: list[tuple[str, float, int]] = []
    for tag, file_ids in index.tag_to_file_ids.items():
        if tag not in ratings or tag.startswith("filename:") or is_filtered_tag(tag):
            continue
        if filter_tag_lower and filter_tag_lower not in tag.lower():
            continue
        score = trueskill_score(ratings[tag])
        if not (filters.score_min <= score <= filters.score_max):
            continue
        file_count = sum(
            1 for fid in file_ids
            if _file_record_passes_filters(tag, index.files.get(fid), filters)
        )
        if file_count < filters.min_files:
            continue
        entries.append((tag, score, file_count))

    return _bucket_top_random_bottom(entries)


def _filters_need_file_metadata(filters: FilterParams) -> bool:
    """Whether any active filter axis actually needs a file's cached metadata to evaluate -
    tag/file service membership, namespace, archive status, date added, resolution, rating
    count. A default/unset value on every one of those axes means "no restriction", so a file
    whose FileRecord never got cached (see _build_index's metadata fetch) can still count
    instead of being silently dropped for a search that never needed its metadata."""
    return bool(
        filters.tag_service_keys or filters.file_service_keys
        or filters.namespace_mode != "all" or filters.archive_mode != "all"
        or filters.date_added_days_ago_min or filters.date_added_days_ago_max is not None
        or filters.aspect_ratio_min > 0.0 or filters.aspect_ratio_max != float("inf")
        or filters.pixel_count_min > 0.0 or filters.pixel_count_max != float("inf")
        or filters.rating_count_min > 0.0 or filters.rating_count_max != float("inf")
    )


def _file_record_passes_filters(tag: str, record: FileRecord | None, filters: FilterParams) -> bool:
    """tag_index.FileRecord equivalent of _file_passes_metadata_filters, extended to also
    cover the axes that used to be baked into the live Hydrus search predicate (tag/file
    service membership, namespace, archive status, date added) - all doable client-side now
    that a full per-file record is cached rather than re-derived per request.

    record can be None when a file matched a tag search but its metadata never made it into
    the index (see tag_index._build_index) - such a file passes here unless some filter axis
    that actually needs metadata is active, rather than being dropped from every filtered
    count unconditionally."""
    if record is None:
        return not _filters_need_file_metadata(filters)
    if filters.tag_service_keys and not any(
        tag in record.tags_by_service.get(tsk, ()) for tsk in filters.tag_service_keys
    ):
        return False
    if filters.file_service_keys and not (record.file_service_keys & set(filters.file_service_keys)):
        return False

    tag_has_namespace = has_namespace(tag)
    if filters.namespace_mode == "namespaced" and not tag_has_namespace:
        return False
    if filters.namespace_mode == "unnamespaced" and tag_has_namespace:
        return False

    if filters.archive_mode == "archived" and record.is_inbox:
        return False
    if filters.archive_mode == "inbox" and not record.is_inbox:
        return False

    relevant_times = (
        [t for k, t in record.import_times.items() if k in filters.file_service_keys]
        if filters.file_service_keys else list(record.import_times.values())
    )
    if relevant_times:
        # Earliest import among the relevant services - "added" means first added anywhere
        # in scope, mirroring the plain (no explicit service filter) common case of one
        # local file service.
        import_time = min(relevant_times)
        days_ago = (time.time() - import_time) / 86400.0
        if filters.date_added_days_ago_max is not None and days_ago > filters.date_added_days_ago_max:
            return False
        if filters.date_added_days_ago_min and days_ago < filters.date_added_days_ago_min:
            return False

    if record.width > 0 and record.height > 0:
        aspect_ratio = record.width / record.height
        pixel_count = record.width * record.height
        if not (filters.aspect_ratio_min <= aspect_ratio <= filters.aspect_ratio_max):
            return False
        if not (filters.pixel_count_min <= pixel_count <= filters.pixel_count_max):
            return False

    rating_count = sum(len(v) for v in record.tags_by_service.values())
    if not (filters.rating_count_min <= rating_count <= filters.rating_count_max):
        return False

    return True


def prompt_for_search(client: hydrus_api.Client | None = None) -> list[str]:
    """Offer a numbered list of top/bottom/random liked tags, or custom. CLI-only (uses input())
    -- rendering wrapper around build_search_options(); see that function for the shared logic."""
    client = client or _get_default_client()
    options = build_search_options(client)

    print("\n=== TagRank Search Selection ===")
    print("Pick a search start point, or 00 for a custom search.")
    if not (options.top or options.random or options.bottom):
        print("(No ratings found yet - falling straight to custom search.)")

    categories = {"Top": options.top, "Random": options.random, "Bottom": options.bottom}
    max_rows = max((len(v) for v in categories.values()), default=0)
    col_width = 40

    print(f"  {'Top':<{col_width}} {'Random':<{col_width}} Bottom")

    def _format(opt: TagOption) -> str:
        parts = opt.tag.split(":", 1)
        if len(parts) == 2:
            main, group = parts
            return f"{opt.index:02d}: [{opt.score:.1f}] {main} ({group}) - {opt.file_count} files"
        return f"{opt.index:02d}: [{opt.score:.1f}] {opt.tag} - {opt.file_count} files"

    for row in range(max_rows):
        cells = [
            _format(categories[label][row]) if row < len(categories[label]) else ""
            for label in ["Top", "Random", "Bottom"]
        ]
        print(f"  {cells[0]:<{col_width}} {cells[1]:<{col_width}} {cells[2]}")

    print("  00: custom search (comma-separated predicates; blank = everything)")

    while True:
        raw = input("\n> ").strip()
        if raw == "00":
            custom = input("Custom search (comma-separated, e.g. '1girl, rating:safe'): ").strip()
            if not custom:
                return ["system:everything"]
            return [p.strip() for p in custom.split(",") if p.strip()]
        try:
            idx = int(raw)
        except ValueError:
            print("  Please enter a number.")
            continue

        tag = options.lookup.get(idx)
        if tag is not None:
            print(f"  Using tag search: {tag}")
            return [tag]

        print("  Selection out of range.")


# ------------------------- pool assembly -------------------------

def get_candidate_seeds(query: list[str], n: int, client: hydrus_api.Client, file_service_key: str = "") -> list[str]:
    """One-shot fetch of up to `n` candidate hashes matching `query`."""
    final_query = query + [f"system:limit = {n}"]
    try:
        kwargs = {"file_service_keys": [file_service_key]} if file_service_key else {}
        resp = client.search_files(final_query, return_hashes=True, **kwargs)
        hashes = list(resp.get("hashes") or [])
        logger.info(f"Fetched {len(hashes)} candidate seeds.")
        return hashes
    except Exception as e:
        logger.error(f"Seed search failed: {e}")
        return []


def build_pool(client: hydrus_api.Client | None = None,
               pool_size: int = POOL_SIZE,
               query: list[str] | None = None,
               file_service_key: str = FILE_SERVICE_KEY,
               use_similarity: bool = True) -> list[str]:
    client = client or _get_default_client()
    if query is None:
        query = _legacy_seed_predicates()
    search_kwargs = {"file_service_keys": [file_service_key]} if file_service_key else {}

    # Similarity search (below) is the slow part - it does a distance-expanding search per
    # seed hash, each a separate Hydrus round-trip. When the caller doesn't need visually-
    # similar neighbors (e.g. Undertow's "Similarity" filter toggle, off by default), skip
    # straight to a plain tag search and randomly sample it down to pool_size instead.
    if not use_similarity:
        try:
            resp = client.search_files(query, return_hashes=True, **search_kwargs)
            hashes = list(resp.get("hashes") or [])
        except Exception as e:
            logger.error(f"Direct search (similarity disabled) failed: {e}")
            return []
        if len(hashes) > pool_size:
            hashes = random.sample(hashes, pool_size)
        logger.info(f"Similarity disabled: using {len(hashes)} file(s) from a direct tag search.")
        return hashes

    logger.info(f"Building pool of {pool_size} files (starting distance: {MAX_DISTANCE_START}, max: {MAX_DISTANCE_HARD})...")

    # If the starting query is a single plain tag with fewer matching files than the
    # requested pool size, similarity-search filtering can't add anything - there's
    # nothing to filter down from. Just use every file that has the tag.
    if len(query) == 1 and not query[0].startswith("system:"):
        tag = query[0]
        available = get_tag_file_count(tag, client, file_service_key)
        if 0 < available < pool_size:
            logger.info(
                f"Tag '{tag}' only has {available} files, fewer than the requested {pool_size}; "
                f"bypassing similarity filtering and using all {available} files."
            )
            try:
                resp = client.search_files(query, return_hashes=True, **search_kwargs)
                return list(resp.get("hashes") or [])
            except Exception as e:
                logger.error(f"Bypass search for tag '{tag}' failed: {e}")
                return []

    candidates = get_candidate_seeds(query, CANDIDATE_SEED_COUNT, client, file_service_key)
    if not candidates:
        logger.error("No candidate seeds returned from API. Aborting.")
        return []

    random.shuffle(candidates)
    seeds = candidates[:SEED_COUNT_FOR_QUERY]
    logger.info(f"Using {len(seeds)} seed hashes for similarity search.")

    pool: list[str] = []
    seen: set[str] = set()
    if MIN_POOL_SATISFIED is None:
        satisfied = pool_size
    else:
        threshold = float(MIN_POOL_SATISFIED)
        if 0.0 <= threshold <= 1.0:
            percent = threshold
        else:
            percent = min(max(threshold, 0.0), 100.0) / 100.0
        satisfied = max(1, int(pool_size * percent))

    hard_stop_start_distance = MAX_DISTANCE_HARD * 2
    seeds_used = 0

    def show_progress(seed_index: int, distance: int) -> None:
        print(
            f"\r  Assembling pool: seed {seed_index + 1}/{len(seeds)}, "
            f"distance {distance} — {len(pool)}/{satisfied} files".ljust(78),
            end="",
            flush=True,
        )

    for seed_index, seed in enumerate(seeds):
        if len(pool) >= pool_size:
            break

        start_distance = MAX_DISTANCE_START + (seed_index * DISTANCE_STEP)
        max_distance_for_seed = MAX_DISTANCE_HARD + (seed_index * DISTANCE_STEP)
        if start_distance > hard_stop_start_distance:
            logger.debug(
                f"Reached hard stop start distance {hard_stop_start_distance}; "
                f"stopping pool expansion after seed {seed_index + 1}/{len(seeds)}."
            )
            break

        distance = start_distance
        seeds_used = seed_index + 1

        while distance <= max_distance_for_seed:
            if len(pool) >= pool_size:
                break

            show_progress(seed_index, distance)

            predicates = list(query)
            predicates.append(f"system:similar to {seed} with distance {distance}")
            predicates.append(f"system:limit = {pool_size * API_LIMIT_FUZZ}")
            try:
                resp = client.search_files(predicates, return_hashes=True, **search_kwargs)
                similar_hashes = list(resp.get("hashes") or [])
            except Exception as e:
                print()
                logger.error(f"Similarity search at distance {distance} failed: {e}")
                return []

            for h in similar_hashes:
                if h == seed or h in seen:
                    continue
                seen.add(h)
                pool.append(h)
                if len(pool) >= pool_size:
                    break

            if len(pool) >= satisfied:
                break

            distance += DISTANCE_STEP

        if len(pool) >= satisfied:
            break

    print()  # end the progress line
    logger.info(f"Pool assembly complete: {len(pool)} files from {seeds_used} seed(s).")
    return pool[:pool_size]


def _legacy_seed_predicates() -> list[str]:
    """Back-compat fallback: read the query from the SEARCH_QUERY setting."""
    return get_list("SEARCH_QUERY", [])


def write_choice(file_hash: str, liked: bool, client: hydrus_api.Client | None = None) -> bool:
    """Persist a like/dislike decision to Hydrus using the supported client API."""
    client = client or _get_default_client()
    try:
        client.set_rating(RATING_SERVICE_KEY, liked, hashes=[file_hash])
        return True
    except Exception as e:
        logger.error(f"Failed to write rating for {file_hash}: {e}")
        return False


def main() -> None:
    """Standalone entry point (ignored when driven by main.py)."""
    ensure_config_files()
    query = prompt_for_search()
    pool = build_pool(query=query)
    if not pool:
        print("Pool is empty. Exiting.")
        return
    print("\n--- TagRank Standalone Session ---")
    print("Controls: [y] Like | [n] Dislike | [q] Quit")
    rated = 0
    for h in pool:
        print(f"\nFile: {h[:16]}...")
        choice = input("> ").strip().lower()
        if choice == 'q':
            break
        if choice in ('y', 'n'):
            if write_choice(h, choice == 'y'):
                print(" +1 (Incremented)" if choice == 'y' else " -1 (Decremented)")
                rated += 1
    print(f"\nSession ended. Rated {rated} files.")


if __name__ == "__main__":
    main()
