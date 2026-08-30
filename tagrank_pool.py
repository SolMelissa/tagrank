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
from pathlib import Path

import hydrus_api

from config import (
    CONFIG_DIR,
    ensure_config_files,
    get_int,
    get_bool,
    get_float_or_none,
    get_list,
    key,
    is_filtered_tag,
)

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
TOP_TAG_OPTIONS      = get_int("TOP_TAG_OPTIONS", 20)
BOTTOM_TAG_OPTIONS   = get_int("BOTTOM_TAG_OPTIONS", 10)
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
    """Load {tag: (mu, sigma)} from ratings.json (TrueSkill params)."""
    try:
        with open(Path("./ratings.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load ratings.json: {e}")
        return {}
    ratings: dict[str, tuple[float, float]] = {}
    for entry in data:
        try:
            tag, (mu, sigma) = entry
            tag_str = str(tag)
            if not is_filtered_tag(tag_str):
                ratings[tag_str] = (float(mu), float(sigma))
        except (ValueError, TypeError):
            continue
    return ratings


def trueskill_score(rating: tuple[float, float]) -> float:
    """scoring used across TagRank: mu - 3*sigma."""
    mu, sigma = rating
    return mu - 3 * sigma


def prompt_for_search() -> list[str]:
    """Offer a numbered list of top/bottom/random liked tags, or custom."""
    ratings = load_ratings()
    ranked = sorted(ratings.items(), key=lambda kv: trueskill_score(kv[1]))

    all_tags = [
        tag for tag, _rating in ranked
        if not tag.startswith("filename:") and not is_filtered_tag(tag)
    ]

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

    categories = {
        "Top": top_tags,
        "Random": random_tags,
        "Bottom": bottom_tags,
    }

    print("\n=== TagRank Search Selection ===")
    print("Pick a search start point, or 00 for a custom search.")
    if not any(categories.values()):
        print("(No ratings found yet - falling straight to custom search.)")

    ordered_rows: list[tuple[str, str]] = []
    for label in ["Top", "Random", "Bottom"]:
        for tag in categories[label]:
            ordered_rows.append((label, tag))

    # column layout
    max_rows = max(len(categories[k]) for k in ("Top", "Random", "Bottom")) if any(categories.values()) else 0
    col_width = 32

    print(f"  {'Top':<{col_width}} {'Random':<{col_width}} Bottom")
    top_list = list(enumerate(categories["Top"], start=1))
    random_list = list(enumerate(categories["Random"], start=1 + len(categories["Top"])))
    bottom_list = list(enumerate(categories["Bottom"], start=1 + len(categories["Top"]) + len(categories["Random"])))

    lookup = {tag: idx for idx, tag in top_list + random_list + bottom_list}
    per_label = {"Top": top_list, "Random": random_list, "Bottom": bottom_list}

    for row in range(max_rows):
        cells = []
        for label in ["Top", "Random", "Bottom"]:
            if row < len(per_label[label]):
                idx, tag = per_label[label][row]
                score = trueskill_score(ratings.get(tag, (0.0, 0.0)))
                parts = tag.split(":", 1)
                if len(parts) == 2:
                    main, group = parts
                    display = f"{idx:02d}: [{score:.1f}] {main} ({group})"
                else:
                    display = f"{idx:02d}: [{score:.1f}] {tag}"
                cells.append(display)
            else:
                cells.append("")
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

        if 1 <= idx <= len(ordered_rows):
            tag = ordered_rows[idx - 1][1]
            print(f"  Using tag search: {tag}")
            return [tag]

        print("  Selection out of range.")


# ------------------------- pool assembly -------------------------

def get_candidate_seeds(query: list[str], n: int, client: hydrus_api.Client) -> list[str]:
    """One-shot fetch of up to `n` candidate hashes matching `query`."""
    final_query = query + [f"system:limit = {n}"]
    try:
        resp = client.search_files(final_query, return_hashes=True)
        hashes = list(resp.get("hashes") or [])
        logger.info(f"Fetched {len(hashes)} candidate seeds.")
        return hashes
    except Exception as e:
        logger.error(f"Seed search failed: {e}")
        return []


def build_pool(client: hydrus_api.Client | None = None,
               pool_size: int = POOL_SIZE,
               query: list[str] | None = None) -> list[str]:
    client = client or _get_default_client()
    if query is None:
        query = _legacy_seed_predicates()

    logger.info(f"🔍 Building pool of {pool_size} files (starting distance: {MAX_DISTANCE_START}, max: {MAX_DISTANCE_HARD})...")

    candidates = get_candidate_seeds(query, CANDIDATE_SEED_COUNT, client)
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
    for seed_index, seed in enumerate(seeds):
        if len(pool) >= pool_size:
            break

        start_distance = MAX_DISTANCE_START + (seed_index * DISTANCE_STEP)
        max_distance_for_seed = MAX_DISTANCE_HARD + (seed_index * DISTANCE_STEP)
        if start_distance > hard_stop_start_distance:
            logger.info(
                f"Reached hard stop start distance {hard_stop_start_distance}; "
                f"stopping pool expansion after seed {seed_index + 1}/{len(seeds)}."
            )
            break

        distance = start_distance
        logger.info(
            f"Starting seed {seed_index + 1}/{len(seeds)} at distance {distance} "
            f"(search range: {distance}..{max_distance_for_seed}): {seed[:12]}..."
        )

        while distance <= max_distance_for_seed:
            if len(pool) >= pool_size:
                break

            predicates = list(query)
            predicates.append(f"system:similar to {seed} with distance {distance}")
            predicates.append(f"system:limit = {pool_size * API_LIMIT_FUZZ}")
            logger.info(f"Searching at distance {distance} for seed {seed[:12]}...")
            try:
                resp = client.search_files(predicates, return_hashes=True)
                similar_hashes = list(resp.get("hashes") or [])
            except Exception as e:
                logger.error(f"Similarity search at distance {distance} failed: {e}")
                return []

            added_this_round = 0
            for h in similar_hashes:
                if h == seed or h in seen:
                    continue
                seen.add(h)
                pool.append(h)
                added_this_round += 1
                if len(pool) >= pool_size:
                    break

            if len(pool) >= satisfied:
                logger.info(f"Pool satisfied at {len(pool)} files; stopping.")
                break

            logger.info(
                f"  Seed {seed[:12]}... collected {added_this_round} new files at distance {distance} "
                f"(total: {len(pool)}/{satisfied}) → escalating to {distance + DISTANCE_STEP}"
            )
            distance += DISTANCE_STEP

        if len(pool) >= satisfied:
            break

        logger.info(
            f"Seed {seed[:12]}... exhausted at distance {distance}; "
            f"next seed starts at {MAX_DISTANCE_START + ((seed_index + 1) * DISTANCE_STEP)}."
        )

    logger.info(f"Pool assembly complete. Total: {len(pool)} files.")
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
