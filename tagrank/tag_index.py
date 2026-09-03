"""In-memory cache of "which files carry each rated tag" plus the per-file metadata the
DB Search filter bar needs (resolution, import time, archive status, tag/file-service
membership) - built once from Hydrus instead of the previous design, where every single
search (initial /search-options, and every DB Search from Undertow's filter bar) re-ran one
live Hydrus round trip per candidate tag, sometimes needing an 180s timeout to finish on a
large library. That per-request cost is what made Undertow's filter bar "just spin" - now
paid once here, and reused. See tagrank_picker.html / undertow's tagrank_client.py for the
UI side of this.

Rating a file doesn't change which files have which tags, so nothing here needs to be
invalidated when a comparison is judged - only a fresh server start (or explicit
refresh_index()) rebuilds this.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import hydrus_api  # type: ignore

from config import is_filtered_tag
from tagrank.hydrus_client import get_file_infos_from_client

logger = logging.getLogger(__name__)


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
    """Forces a full rebuild - e.g. after new tags get rated, or files get added/removed in
    Hydrus, and the cache needs to catch up. Not called automatically."""
    global _index
    with _lock:
        _index = _build_index(client)
    return _index


def _build_index(client: hydrus_api.Client) -> TagIndex:
    from tagrank.pool import load_ratings  # local import: avoid a cycle (pool imports us)

    ratings = load_ratings()
    candidate_tags = [
        tag for tag in ratings
        if not tag.startswith("filename:") and not is_filtered_tag(tag)
    ]
    logger.info(f"Building TagRank tag index for {len(candidate_tags)} rated tag(s)...")

    tag_to_file_ids: dict[str, set[int]] = {}
    all_file_ids: set[int] = set()
    for tag in candidate_tags:
        try:
            resp = client.search_files([tag], return_file_ids=True)
            ids = set(resp.get("file_ids") or [])
        except Exception as e:  # noqa: BLE001 - one bad tag shouldn't abort the whole index
            logger.error(f"Tag index: search for '{tag}' failed: {e}")
            ids = set()
        tag_to_file_ids[tag] = ids
        all_file_ids.update(ids)

    files: dict[int, FileRecord] = {}
    if all_file_ids:
        for file_id, metadata in get_file_infos_from_client(client, list(all_file_ids)):
            files[file_id] = _to_file_record(metadata)

    logger.info(f"Tag index built: {len(candidate_tags)} tag(s), {len(files)} distinct file(s).")
    return TagIndex(tag_to_file_ids=tag_to_file_ids, files=files)


def _to_file_record(metadata: dict) -> FileRecord:
    tags_by_service: dict[str, set[str]] = {}
    for service_key, service_data in (metadata.get("tags") or {}).items():
        tags_by_service[service_key] = set((service_data.get("display_tags") or {}).get("0", []))

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
