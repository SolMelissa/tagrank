"""In-memory cache of "which files carry each rated tag" plus the per-file metadata the
DB Search filter bar needs (resolution, import time, archive status, tag/file-service
membership) - built once from Hydrus instead of the previous design, where every single
search (initial /search-options, and every DB Search from Undertow's filter bar) re-ran one
live Hydrus round trip per candidate tag, sometimes needing an 180s timeout to finish on a
large library. That per-request cost is what made Undertow's filter bar "just spin" - now
paid once here, and reused. See tagrank_picker.html / undertow's tagrank_client.py for the
UI side of this.

Built as a handful of OR-batched searches (files carrying ANY rated tag) plus one chunked
metadata fetch over just that result set - not one search per rated tag, and not a scan of the
whole library. Two earlier versions of this file got this wrong in opposite directions: the
original searched once per tag (O(rated tags) separate Hydrus searches - the better part of an
hour with a few thousand rated tags, even parallelized across workers), and the version right
before this one fixed that by pulling every file in the library and inverting locally - correct,
but "every file" means all 500k+ of them on a large install, most of which don't carry any rated
tag at all, so it still fetched far more than it needed.

Hydrus's search API accepts a nested (un-prefixed) list within the top-level tags list as an OR
group - see developer_api.html's "OR predicates" section: `["skirt", ["samus aran", "lara
croft"]]` means `skirt AND (samus aran OR lara croft)`. Batching a few hundred rated tags per OR
group keeps each request's predicate list and result set bounded, turns ~1760 tags into single-
digit search round trips. On a library where most files carry at least one rated tag (a broad
rated tag like a performer name can cover a huge fraction of a large collection), that OR search
still resolves to nearly every file, so it alone doesn't bound the metadata-fetch cost - see the
on-disk file cache below for what actually does.

Per-file metadata (FileRecord) is cached to disk between runs, keyed by file_id, so a later
build only needs to fetch metadata for files not already known - typically just whatever's been
imported since the last run - rather than paying the full per-file Hydrus round trip cost every
single startup. This trades a small amount of staleness for that speed: if an already-cached
file gets re-tagged in Hydrus without any new file being added, that file's cached tags won't
reflect the change until refresh_index() is called explicitly (which always bypasses the cache
and re-fetches everything) or the cache file is deleted. Rating a file in TagRank doesn't change
which files have which tags, so nothing here needs invalidating just because a comparison was
judged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field

import hydrus_api  # type: ignore

from config import DATA_DIR, is_excluded_tag, get
from tagrank.hydrus_client import get_file_infos_from_client
from tagrank.tag_utils import has_namespace, resolve_tags

logger = logging.getLogger(__name__)

# How many rated tags go into one OR-group search. Kept well under any practical URL/JSON size
# concern while still turning ~1760 tags into single-digit round trips instead of one per tag.
_OR_SEARCH_BATCH_SIZE = 256

_FILE_CACHE_PATH = DATA_DIR / "tag_index_file_cache.json"
_FILE_CACHE_VERSION = 1

# The cache is saved after every this-many newly-fetched files, not just once at the end of a
# build - a cold build on a large library can be many thousands of Hydrus round trips, and
# without checkpointing, killing the process partway through (a crash, a forced daemon restart,
# closing the TagRank tab mid-build) would lose everything fetched in that run, forcing the next
# build to start over from whatever was cached before *this* run began. 5000 keeps checkpoint
# writes infrequent enough to not dominate build time while bounding how much re-fetch work an
# interruption can cost to a few checkpoints' worth.
_CACHE_CHECKPOINT_SIZE = 5000


@dataclass
class FileRecord:
    width: int
    height: int
    is_inbox: bool
    tags_by_service: dict[str, set[str]] = field(default_factory=dict)
    import_times: dict[str, float] = field(default_factory=dict)  # file_service_key -> unix time
    file_service_keys: set[str] = field(default_factory=set)


@dataclass
class TagIndex:
    tag_to_file_ids: dict[str, set[int]]
    files: dict[int, FileRecord]


_index: TagIndex | None = None
_lock = threading.Lock()


def get_index() -> TagIndex | None:
    """The currently-cached index, or None if it hasn't been built yet (server just started
    and the eager startup build in server.py hasn't finished, or failed)."""
    return _index


def ensure_index(client: hydrus_api.Client) -> TagIndex:
    """Builds the index on first call and reuses it afterwards. Safe to call from every
    request handler - only the first caller (or the eager startup build) pays the cost."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is None:
            _index = _build_index(client)
    return _index


def refresh_index(client: hydrus_api.Client) -> TagIndex:
    """Forces a full rebuild that bypasses the on-disk file cache entirely - e.g. after
    re-tagging files in Hydrus in a way that would leave cached FileRecords stale. Not called
    automatically. A plain server restart does NOT do this - see ensure_index/_build_index."""
    global _index
    with _lock:
        _index = _build_index(client, force_refresh=True)
    return _index


def _build_index(client: hydrus_api.Client, *, force_refresh: bool = False) -> TagIndex:
    from tagrank import hidden_tags  # local import: avoid a cycle
    from tagrank.pool import load_ratings  # local import: avoid a cycle (pool imports us)

    # Refresh hidden tags on each build so changes in Hydrus are picked up
    hidden_tags.refresh_hidden_tags(client)

    ratings = load_ratings()
    candidate_tags = {
        tag for tag in ratings
        if not tag.startswith("filename:") and not is_excluded_tag(tag)
    }

    # Apply namespace filter if configured
    tag_universe = get("TAG_UNIVERSE", "all").strip().lower()
    if tag_universe == "namespaced_only":
        candidate_tags = {tag for tag in candidate_tags if has_namespace(tag)}
        logger.info(f"TAG_UNIVERSE=namespaced_only: filtered to {len(candidate_tags)} namespaced tag(s)")

    logger.info(f"Building TagRank tag index for {len(candidate_tags)} rated tag(s)...")

    ordered_tags = list(candidate_tags)
    all_file_ids: set[int] = set()
    for start in range(0, len(ordered_tags), _OR_SEARCH_BATCH_SIZE):
        batch = ordered_tags[start:start + _OR_SEARCH_BATCH_SIZE]
        # A single-element tags list whose element is itself a list is Hydrus's OR-group
        # syntax - this finds every file carrying at least one tag in `batch`, in one request.
        resp = client.search_files([batch], return_file_ids=True)
        all_file_ids.update(resp.get("file_ids") or [])
        done = min(start + _OR_SEARCH_BATCH_SIZE, len(ordered_tags))
        logger.info(f"Tag index: searched {done}/{len(ordered_tags)} tag(s), {len(all_file_ids)} file(s) found so far...")

    cached_files = {} if force_refresh else _load_file_cache()
    reused_ids = all_file_ids & cached_files.keys()
    new_ids = all_file_ids - cached_files.keys()
    logger.info(
        f"Tag index: {len(all_file_ids)} file(s) carry a rated tag "
        f"({len(reused_ids)} reused from local cache, {len(new_ids)} new - fetching from Hydrus)..."
    )

    # Persisting only the file_ids actually in `files`, not the raw union with whatever was
    # loaded, means a file that dropped out of every rated tag (re-tagged, deleted) since the
    # cache was last written drops out of the cache too, not lingers forever - true from this
    # very first save (before any new fetching happens) since `reused_ids` already excludes
    # anything no longer in `all_file_ids`.
    files: dict[int, FileRecord] = {file_id: cached_files[file_id] for file_id in reused_ids}
    new_id_list = list(new_ids)
    if new_id_list:
        for start in range(0, len(new_id_list), _CACHE_CHECKPOINT_SIZE):
            batch_ids = new_id_list[start:start + _CACHE_CHECKPOINT_SIZE]
            for file_id, metadata in get_file_infos_from_client(client, batch_ids):
                files[file_id] = _to_file_record(metadata)
            # Checkpoint: everything fetched up to here survives an interruption from this
            # point on, even if the build never reaches the end of new_id_list.
            _save_file_cache(files)
            done = min(start + _CACHE_CHECKPOINT_SIZE, len(new_id_list))
            logger.info(f"Tag index: fetched and cached {done}/{len(new_id_list)} new file(s)...")
    else:
        _save_file_cache(files)

    tag_to_file_ids: dict[str, set[int]] = {tag: set() for tag in candidate_tags}
    for file_id, record in files.items():
        file_tags: set[str] = set()
        for tags in record.tags_by_service.values():
            file_tags.update(tags)
        for tag in file_tags & candidate_tags:
            tag_to_file_ids[tag].add(file_id)

    logger.info(f"Tag index built: {len(candidate_tags)} tag(s), {len(files)} distinct file(s).")
    return TagIndex(tag_to_file_ids=tag_to_file_ids, files=files)


def _load_file_cache() -> dict[int, FileRecord]:
    """Best-effort load of the on-disk file cache - any problem (missing file, corrupt JSON, a
    schema version bump) just means starting cold and re-fetching everything from Hydrus, same
    as if caching didn't exist."""
    try:
        with open(_FILE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.info(f"Tag index: no usable file cache ({e}) - building cold.")
        return {}
    if data.get("version") != _FILE_CACHE_VERSION:
        logger.info("Tag index: file cache is from an older schema version - building cold.")
        return {}
    cached: dict[int, FileRecord] = {}
    for file_id_str, record_data in (data.get("files") or {}).items():
        try:
            cached[int(file_id_str)] = _file_record_from_json(record_data)
        except (ValueError, TypeError, KeyError):
            continue
    return cached


def _save_file_cache(files: dict[int, FileRecord]) -> None:
    """Best-effort save - a failure here (disk full, permissions) shouldn't take down an
    otherwise-successful index build, just means next startup pays the fetch cost again.

    Writes to a temp file and atomically renames it over the real cache path (os.replace is
    atomic on both POSIX and Windows) rather than writing the real path directly. A build now
    checkpoints this on every batch of newly-fetched files (see _CACHE_CHECKPOINT_SIZE), so a
    plain truncate-then-write here would mean a kill during any one of those many writes could
    corrupt the cache and lose everything from every previous successful run too, not just this
    one's progress - the exact failure mode checkpointing exists to avoid."""
    data = {
        "version": _FILE_CACHE_VERSION,
        "files": {str(file_id): _file_record_to_json(record) for file_id, record in files.items()},
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _FILE_CACHE_PATH.with_suffix(_FILE_CACHE_PATH.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, _FILE_CACHE_PATH)
    except OSError as e:
        logger.error(f"Tag index: couldn't save file cache: {e}")


def _file_record_to_json(record: FileRecord) -> dict:
    return {
        "width": record.width,
        "height": record.height,
        "is_inbox": record.is_inbox,
        "tags_by_service": {key: sorted(tags) for key, tags in record.tags_by_service.items()},
        "import_times": record.import_times,
        "file_service_keys": sorted(record.file_service_keys),
    }


def _file_record_from_json(data: dict) -> FileRecord:
    return FileRecord(
        width=int(data.get("width") or 0),
        height=int(data.get("height") or 0),
        is_inbox=bool(data.get("is_inbox")),
        tags_by_service={key: set(tags) for key, tags in (data.get("tags_by_service") or {}).items()},
        import_times=dict(data.get("import_times") or {}),
        file_service_keys=set(data.get("file_service_keys") or []),
    )


def _to_file_record(metadata: dict) -> FileRecord:
    tags_by_service: dict[str, set[str]] = {}
    for service_key, service_data in (metadata.get("tags") or {}).items():
        # Use resolve_tags to get final tag set (respects siblings + namespaced carve-out)
        resolved = resolve_tags(service_data)
        # Filter out excluded tags (TAG_FILTERS + hidden-tags marker)
        tags_by_service[service_key] = {tag for tag in resolved if not is_excluded_tag(tag)}

    file_services = (metadata.get("file_services") or {}).get("current", {}) or {}
    import_times = {key: (data.get("time_imported") or 0) for key, data in file_services.items()}

    return FileRecord(
        width=metadata.get("width") or 0,
        height=metadata.get("height") or 0,
        is_inbox=bool(metadata.get("is_inbox")),
        tags_by_service=tags_by_service,
        import_times=import_times,
        file_service_keys=set(file_services.keys()),
    )
