#!/usr/bin/env python3
"""One-time setup helper: create a hidden-tags marker file in Hydrus and set TAGRANK_HIDDEN_TAGS_FILE_HASH.

This script:
1. Generates a small placeholder image (1x1 PNG or simple text-rendered PNG)
2. Imports it into Hydrus via the Client API
3. Sets an explanatory note on the file
4. Prints the file's SHA256 hash for the user to paste into config/KEYS

Run once when setting up TagRank's hidden-tags feature:
    python -m tagrank.setup_hidden_tags_marker
"""

import io
import logging
import sys
from pathlib import Path

import hydrus_api  # type: ignore

from config import key, set_and_persist_key
from tagrank.hydrus_client import create_client
from tagrank.settings import load_settings

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def create_placeholder_image() -> bytes:
    """Generate a minimal placeholder image (1x1 transparent PNG)."""
    # Minimal valid 1x1 PNG (transparent)
    png_bytes = bytes([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,  # PNG signature
        0x00, 0x00, 0x00, 0x0d,                            # IHDR chunk size
        0x49, 0x48, 0x44, 0x52,                            # IHDR
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,  # 1x1
        0x08, 0x06, 0x00, 0x00, 0x00,                     # 8-bit RGBA
        0x1f, 0x15, 0xc4, 0x89,                            # CRC
        0x00, 0x00, 0x00, 0x0a,                            # IDAT chunk size
        0x49, 0x44, 0x41, 0x54,                            # IDAT
        0x78, 0x9c, 0x63, 0xf8, 0xcf, 0xc0, 0x00, 0x00,  # Deflated data
        0x00, 0x03, 0x00, 0x01,                            # Minimal zlib
        0xa4, 0x68, 0x9b, 0xb0,                            # CRC
        0x00, 0x00, 0x00, 0x00,                            # IEND chunk size
        0x49, 0x45, 0x4e, 0x44,                            # IEND
        0xae, 0x42, 0x60, 0x82,                            # CRC
    ])
    return png_bytes


def main() -> None:
    """Create marker file, import it, set note, print hash."""
    try:
        settings = load_settings()
        client = create_client(settings)
    except Exception as e:
        logger.error(f"Could not connect to Hydrus: {e}")
        sys.exit(1)

    # Create placeholder
    logger.info("Generating placeholder image...")
    png_data = create_placeholder_image()

    # Import via temp file (Client API's add_file expects a path or file-like object)
    temp_file = Path("/tmp/tagrank_hidden_tags_marker.png") if sys.platform != "win32" else Path("tagrank_hidden_tags_marker.png")
    try:
        temp_file.write_bytes(png_data)
        logger.info(f"Importing placeholder into Hydrus ({temp_file})...")
        try:
            client.add_file(str(temp_file), delete_after_successful_import=True)
        except Exception as e:
            logger.error(f"Import failed: {e}")
            temp_file.unlink(missing_ok=True)
            sys.exit(1)
    finally:
        temp_file.unlink(missing_ok=True)

    # The newly-imported file is in the inbox; fetch it to get its hash
    logger.info("Fetching newly-imported file...")
    try:
        resp = client.search_files(tags=["system:inbox"], return_file_ids=True)
        file_ids = resp.get("file_ids") or []
        if not file_ids:
            logger.error("Could not find imported file (inbox search returned nothing).")
            sys.exit(1)

        # Get metadata for all inbox files, find the one we just added (smallest size, most recent)
        metadata_resp = client.get_file_metadata(file_ids=file_ids[-10:])  # Check last 10 inbox files
        newest_candidate = None
        for info in (metadata_resp.get("metadata") or []):
            if info.get("file_size") < 1000:  # Our placeholder is tiny
                if newest_candidate is None or info.get("file_id", 0) > newest_candidate.get("file_id", 0):
                    newest_candidate = info

        if not newest_candidate:
            logger.error(f"Could not identify the newly-imported marker file among inbox files.")
            sys.exit(1)

        file_hash = newest_candidate.get("file_hash")
        if not file_hash:
            logger.error("Imported file has no hash?")
            sys.exit(1)

        logger.info(f"Found marker file: {file_hash}")
    except Exception as e:
        logger.error(f"Could not find imported file: {e}")
        sys.exit(1)

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
        logger.info("  You can now tag the marker file in Hydrus with tags you want TagRank to ignore.")
    except Exception as e:
        logger.error(f"Could not save hash to config: {e}. You can set it manually:")
        logger.error(f"  1. Open config/KEYS")
        logger.error(f"  2. Set: TAGRANK_HIDDEN_TAGS_FILE_HASH = {file_hash}")
        sys.exit(1)


if __name__ == "__main__":
    main()
