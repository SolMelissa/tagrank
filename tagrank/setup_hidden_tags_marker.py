#!/usr/bin/env python3
"""One-time setup helper: import the pre-generated hidden-tags marker image into Hydrus.

This script:
1. Imports the pre-generated marker image from tagrank/assets/
2. Tags it with "service:tagrank" and "service:undertow" for easy discovery
3. Sets an explanatory note on the file
4. Saves the file's SHA256 hash to config/KEYS as TAGRANK_HIDDEN_TAGS_FILE_HASH

Run once when setting up TagRank's hidden-tags feature:
    python -m tagrank.setup_hidden_tags_marker
"""

import logging
import sys
import time
from pathlib import Path

import hydrus_api  # type: ignore

from config import key, set_and_persist_key
from tagrank.hydrus_client import create_client
from tagrank.settings import load_settings

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def get_marker_image_path() -> Path:
    """Get the path to the pre-generated marker image."""
    # tagrank/assets/tagrank_hidden_tags_marker.png
    script_dir = Path(__file__).parent  # tagrank/
    asset_path = script_dir.parent / "tagrank" / "assets" / "tagrank_hidden_tags_marker.png"
    if not asset_path.exists():
        # Fallback: try relative to current directory
        asset_path = Path("tagrank/assets/tagrank_hidden_tags_marker.png")
    return asset_path


def main() -> None:
    """Import marker image, tag it, set note, and save hash to config."""
    try:
        settings = load_settings()
        client = create_client(settings)
    except Exception as e:
        logger.error(f"Could not connect to Hydrus: {e}")
        sys.exit(1)

    # Locate the pre-generated marker image
    asset_path = get_marker_image_path()
    if not asset_path.exists():
        logger.error(f"Marker image not found at {asset_path}")
        logger.error("Make sure you're running this from the tagrank repo root.")
        sys.exit(1)

    logger.info(f"Importing marker image from {asset_path}...")
    try:
        add_resp = client.add_file(str(asset_path), delete_after_successful_import=False)
    except Exception as e:
        logger.error(f"Import failed: {e}")
        sys.exit(1)

    # Extract the file hash from the import response
    logger.info("Fetching newly-imported file...")
    import_hash = None
    if isinstance(add_resp, dict) and "hash" in add_resp:
        import_hash = add_resp["hash"]
        logger.info(f"Got hash from import: {import_hash}")

    # Wait a moment for Hydrus to fully index the imported file
    time.sleep(2)

    try:
        marker_file = None

        # If we got the hash from import, use it directly
        if import_hash:
            logger.info(f"Looking up file with hash {import_hash}...")
            metadata_resp = client.get_file_metadata(hashes=[import_hash])
            if metadata_resp and metadata_resp.get("metadata"):
                for info in metadata_resp.get("metadata", []):
                    if info.get("width") == 500 and info.get("height") == 350:
                        marker_file = info
                        break

        # Fallback: search the last 1000 files in inbox (more manageable than all 533k)
        if not marker_file:
            logger.info("Hash lookup failed or returned no results. Searching recent inbox files...")
            resp = client.search_files(tags=["system:inbox"], return_file_ids=True)
            file_ids = resp.get("file_ids") or []
            if not file_ids:
                logger.error("Could not find imported file (inbox search returned nothing).")
                sys.exit(1)

            # Check only the most recently-added files (last 1000) for performance
            recent_file_ids = file_ids[-1000:] if len(file_ids) > 1000 else file_ids
            logger.info(f"Checking {len(recent_file_ids)} most recent inbox files...")
            metadata_resp = client.get_file_metadata(file_ids=recent_file_ids)

            for info in (metadata_resp.get("metadata") or []):
                # Marker image is 500x350, which is distinctive
                if info.get("width") == 500 and info.get("height") == 350:
                    if marker_file is None or info.get("file_id", 0) > marker_file.get("file_id", 0):
                        marker_file = info

        if not marker_file:
            logger.error("Could not identify the marker image in inbox (looking for 500x350 PNG).")
            sys.exit(1)

        logger.info(f"Found marker file with correct dimensions (500x350).")

        # Use the hash we got from import, or extract it from metadata
        file_hash = import_hash or marker_file.get("file_hash") or marker_file.get("hash")
        if not file_hash:
            logger.error("Imported file has no hash?")
            sys.exit(1)

        logger.info(f"Found marker file: {file_hash}")
    except Exception as e:
        logger.error(f"Could not find imported file: {e}")
        sys.exit(1)

    # Tag the marker file with service tags for easy discovery (optional)
    tags_added = False
    tag_service_key = key("TAG_SERVICE_KEY", "").strip()
    if tag_service_key and tag_service_key != "FILL_ME_IN":
        logger.info(f"Tagging marker file with service:tagrank and service:undertow...")
        try:
            file_id = marker_file.get("file_id")
            if file_id:
                client.add_tags(
                    file_ids=[file_id],
                    service_keys_to_actions_to_tags={
                        tag_service_key: {
                            hydrus_api.TagAction.ADD: ["service:tagrank", "service:undertow"]
                        }
                    }
                )
                tags_added = True
                logger.info("✓ Tagged with service:tagrank and service:undertow")
        except Exception as e:
            logger.info(f"Note: Could not tag marker file: {e}")
    else:
        logger.info("Tagging skipped (no TAG_SERVICE_KEY configured)")

    # Set explanatory note
    try:
        note_text = (
            "TagRank hidden-tags marker file. Every tag applied to this file is treated by TagRank "
            "as hidden: never read, cached, rated, or shown in search. Add/remove tags here the same way "
            "you would on any file. Do not delete this file or TagRank's TAGRANK_HIDDEN_TAGS_FILE_HASH "
            "setting will need updating."
        )
        client.set_notes(notes={"TagRank": note_text}, hash_=file_hash)
        logger.info("Set explanatory note on marker file.")
    except Exception as e:
        logger.warning(f"Could not set note (file still usable): {e}")

    # Save hash to config
    logger.info(f"Saving file hash to config/KEYS...")
    try:
        set_and_persist_key("TAGRANK_HIDDEN_TAGS_FILE_HASH", file_hash)
        logger.info("✓ Setup complete! Hidden-tags feature is now active.")
        logger.info(f"  Marker file hash: {file_hash}")
        if tags_added:
            logger.info("  Marker file is tagged with: service:tagrank, service:undertow")
            logger.info("  You can search for it in Hydrus with: service:tagrank or service:undertow")
        else:
            logger.info("  Marker file can be found manually in your Hydrus inbox")
        logger.info("")
        logger.info("  To hide tags from TagRank, tag the marker file with those tags (e.g., 'house', 'rating:safe')")
        logger.info("  To sync hidden tags with the marker file, run:")
        logger.info("    python -m tagrank.sync_hidden_tags_to_marker")
    except Exception as e:
        logger.error(f"Could not save hash to config: {e}. You can set it manually:")
        logger.error(f"  1. Open config/KEYS")
        logger.error(f"  2. Set: TAGRANK_HIDDEN_TAGS_FILE_HASH = {file_hash}")
        sys.exit(1)


if __name__ == "__main__":
    main()
