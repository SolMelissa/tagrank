"""Hidden-tag marker file sync with Hydrus.

The user creates a dedicated file in Hydrus and tags it with every tag that should be hidden
from TagRank (via the normal "manage tags" dialog). TagRank reads that file once at startup
(and on explicit refresh) and treats those tags as excluded from all ratings, caching, and
searches. See config/KEYS's TAGRANK_HIDDEN_TAGS_FILE_HASH setting for the marker file's hash.
"""

import logging
import threading
from typing import Any

import hydrus_api  # type: ignore

from config import key

logger = logging.getLogger(__name__)

_hidden_tags: set[str] | None = None
_lock = threading.Lock()


def refresh_hidden_tags(client: hydrus_api.Client) -> set[str]:
    """Fetch the hidden-tag marker file's tags and update the cache.

    Best-effort: if the file hash is not set, missing, or unreadable, logs a warning and
    returns/caches an empty set. Never raises, so TagRank can always start even if the
    marker file is unavailable or deleted.

    Args:
        client: Hydrus API client.

    Returns:
        Set of tag strings to hide from TagRank (may be empty if feature is inactive or
        the file is not found).
    """
    global _hidden_tags

    file_hash = key("TAGRANK_HIDDEN_TAGS_FILE_HASH", "").strip()
    if not file_hash or file_hash == "FILL_ME_IN":
        logger.info("Hidden tags feature not configured (TAGRANK_HIDDEN_TAGS_FILE_HASH not set).")
        with _lock:
            _hidden_tags = set()
        return _hidden_tags

    try:
        resp = client.get_file_metadata(hashes=[file_hash], only_return_basic_information=False)
        if not resp or not resp.get("metadata"):
            logger.warning(
                f"Hidden-tags marker file not found (hash={file_hash}). "
                "Create it in Hydrus and set TAGRANK_HIDDEN_TAGS_FILE_HASH in config/KEYS."
            )
            with _lock:
                _hidden_tags = set()
            return _hidden_tags

        hidden_set: set[str] = set()
        for file_info in resp["metadata"]:
            for service_key, service_data in (file_info.get("tags") or {}).items():
                if service_data and "display_tags" in service_data:
                    for status in ("0", "1"):
                        if status in service_data["display_tags"]:
                            hidden_set.update(service_data["display_tags"][status])

        logger.info(f"Refreshed hidden tags from marker file: {len(hidden_set)} tag(s).")
        with _lock:
            _hidden_tags = hidden_set
        return _hidden_tags

    except (hydrus_api.ServerError, hydrus_api.ConnectionError) as e:
        logger.warning(
            f"Could not fetch hidden-tags marker file (hash={file_hash}, error={e}). "
            "Proceeding with empty hidden set."
        )
        with _lock:
            _hidden_tags = set()
        return _hidden_tags


def is_hidden_tag(tag: str) -> bool:
    """Check if a tag is in the current hidden set.

    Thread-safe. Returns False if the hidden-tags feature has not been refreshed yet
    (cache is None), so early calls before refresh_hidden_tags is never reached return
    False (safe-default: don't hide tags that might not actually be hidden).

    Args:
        tag: Tag string to check.

    Returns:
        True if the tag is in the hidden set.
    """
    with _lock:
        if _hidden_tags is None:
            return False
        return tag in _hidden_tags


def get_hidden_tags() -> set[str]:
    """Return a copy of the current hidden-tags set (read-only access for diagnostics).

    Thread-safe. Returns empty set if not yet refreshed.
    """
    with _lock:
        return set(_hidden_tags or set())
