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
the dataset from the SEARCH_QUERY file, so editing that file no longer silently
swaps the pool.

Pool assembly
-------------
Similarity search uses hydrus's perceptual-hash engine via the system predicate
`system:similar to <hash> distance <n>`. Multiple such predicates fold into ONE
/search_files call capped with `system:limit = pool_size * fuzz`, so hydrus
returns ~pool_size similar files in a single round-trip (no 40x seed re-queries).
"""

import json
import logging
import random
from pathlib import Path

import hydrus_api

# ============================ CONFIG ============================
API_URL            = "http://127.0.0.1:45869/"
API_KEY            = "f7b95d14bc3a1d9519a316a4e8b111b66b2f368b4c180ef7811f7be6b7bef552"
RATING_SERVICE_KEY = "de2e8e89a036355929f7ba9947ea5bdfd6978a548feed24f5cd639dc24c37f0a"

POOL_SIZE            = 100          # final comparison pool size
MAX_DISTANCE         = 10           # upper edge; used in the system:similar to predicate
CANDIDATE_SEED_COUNT = 10000           # diverse candidates fetched once
SEED_COUNT_FOR_QUERY = 10           # how many seeds fold into the combined query
API_LIMIT_FUZZ       = 2            # over-fetch multiplier to survive dedup
TOP_TAG_OPTIONS      = 20           # how many "most liked" tags to offer

DEBUG_MODE = True
# ================================================================

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
        if DEBUG_MODE:
            logger.warning(f"[DEBUG] Could not load ratings.json: {e}")
        return {}
    ratings: dict[str, tuple[float, float]] = {}
    for entry in data:
        try:
            tag, (mu, sigma) = entry
            ratings[str(tag)] = (float(mu), float(sigma))
        except (ValueError, TypeError):
            continue
    return ratings


def trueskill_score(rating: tuple[float, float]) -> float:
    """scoring used across TagRank: mu - 3*sigma."""
    mu, sigma = rating
    return mu - 3 * sigma


def prompt_for_search() -> list[str]:
    """Offer the user a numbered list of their most-liked tags (0 = custom)."""
    ratings = load_ratings()
    ranked = sorted(ratings.items(), key=lambda kv: trueskill_score(kv[1]), reverse=True)
    top = ranked[:TOP_TAG_OPTIONS]

    print("\n=== TagRank Search Selection ===")
    print("Pick a search start point (your most-liked tags), or 0 for a custom search.")
    if not top:
        print("(No ratings found yet - falling straight to custom search.)")
    for i, (tag, _rating) in enumerate(top, start=1):
        print(f"  {i}: {tag}")
    print("  0: custom search (comma-separated predicates; blank = everything)")

    while True:
        raw = input("\n> ").strip()
        if raw == "0":
            custom = input("Custom search (comma-separated, e.g. '1girl, rating:safe'): ").strip()
            if not custom:
                return ["system:everything"]
            return [p.strip() for p in custom.split(",") if p.strip()]
        try:
            idx = int(raw)
        except ValueError:
            print("  Please enter a number.")
            continue
        if 1 <= idx <= len(top):
            tag = top[idx - 1][0]
            print(f"  Using tag search: {tag}")
            return [tag]
        print("  Selection out of range.")


# ------------------------- pool assembly -------------------------

def get_candidate_seeds(query: list[str], n: int, client: hydrus_api.Client) -> list[str]:
    """One-shot fetch of up to `n` candidate hashes matching `query`."""
    final_query = query + [f"system:limit = {n}"]
    if DEBUG_MODE:
        logger.info(f"[DEBUG] candidate seed query: {final_query}")
    try:
        resp = client.search_files(final_query, return_hashes=True)
        hashes = list(resp.get("hashes") or [])
        if DEBUG_MODE:
            logger.info(f"[DEBUG] candidate seed search returned {len(hashes)} hashes")
        return hashes
    except Exception as e:
        logger.error(f"Seed search failed: {e}")
        return []


def build_pool(client: hydrus_api.Client | None = None,
               pool_size: int = POOL_SIZE,
               query: list[str] | None = None) -> list[str]:
    """Assemble a pool of up to `pool_size` visually-similar hashes.

    `query` selects the starting seed set. If None, falls back to SEARCH_QUERY.
    """
    client = client or _get_default_client()
    if query is None:
        query = _legacy_seed_predicates()

    logger.info(f"Building pool of {pool_size} files (Distance: 0-{MAX_DISTANCE})...")

    candidates = get_candidate_seeds(query, CANDIDATE_SEED_COUNT, client)
    if not candidates:
        logger.error("No candidate seeds returned from API. Aborting.")
        return []

    random.shuffle(candidates)
    seeds = candidates[:SEED_COUNT_FOR_QUERY]
    if DEBUG_MODE:
        logger.info(f"[DEBUG] using {len(seeds)} seeds for the combined similarity query")

    seed_str = " ".join(seeds)
    predicates = [f"system:similar to {seed_str} with distance {MAX_DISTANCE}",
        f"system:limit = {pool_size * API_LIMIT_FUZZ}"]

    predicates.append(f"system:limit = {pool_size * API_LIMIT_FUZZ}")

    try:
        resp = client.search_files(predicates, return_hashes=True)
        similar_hashes = list(resp.get("hashes") or [])
    except Exception as e:
        logger.error(f"Combined similarity search failed: {e}")
        return []

    # Dedupe, drop seeds themselves, and cap at pool_size (order preserved).
    pool: list[str] = []
    seen: set[str] = set(seeds)
    for h in similar_hashes:
        if h in seen:
            continue
        seen.add(h)
        pool.append(h)
        if len(pool) >= pool_size:
            break

    logger.info(f"Pool assembly complete. Total: {len(pool)} files.")
    return pool


def _legacy_seed_predicates() -> list[str]:
    """Back-compat fallback: read the SEARCH_QUERY file."""
    try:
        with open("SEARCH_QUERY", "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []


def write_choice(file_hash: str, liked: bool, client: hydrus_api.Client | None = None) -> bool:
    """Persist a like/dislike decision to Hydrus."""
    client = client or _get_default_client()
    try:
        resp = client.request("POST", "/edit_ratings/set_rating", json={
            "service_key": RATING_SERVICE_KEY,
            "hashes": [file_hash],
            "rating": "increment" if liked else "decrement",
        })
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to write rating for {file_hash}: {e}")
        return False


def main() -> None:
    """Standalone entry point (ignored when driven by main.py)."""
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
