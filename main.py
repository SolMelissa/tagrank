#!/usr/bin/env python3
"""TagRank entrypoint. Run with `python main.py` (rank tags),
`python main.py --create_image_ranking` (build the sort order), or
`python main.py --serve [--port N]` (headless HTTP API, see docs/api.md)."""

import sys

from tagrank.app import MODE_CREATE_IMAGE_RANKING, MODE_RANK_TAGS, MODE_SERVE, main

if __name__ == "__main__":
    arguments = sys.argv
    if "--create_image_ranking" in arguments:
        mode = MODE_CREATE_IMAGE_RANKING
    elif "--serve" in arguments:
        mode = MODE_SERVE
    else:
        mode = MODE_RANK_TAGS

    port = 8420
    if "--port" in arguments:
        port_index = arguments.index("--port")
        if port_index + 1 < len(arguments):
            port = int(arguments[port_index + 1])

    main(mode, port=port)
