#!/usr/bin/env python3
"""TagRank entrypoint. Run with `python main.py` (rank tags),
`python main.py --create_image_ranking` (build the sort order),
`python main.py --serve [--port N]` (headless HTTP API, see docs/api.md), or
`python main.py --tag <tag>` (rank tags, skipping the interactive search picker in favor of
that one tag - for external launchers, e.g. Undertow's TagRank tab, that already know which
tag they want). Add `--no-similarity` to skip the visual-similarity pool expansion and use a
plain tag search instead (much faster; Undertow's TagRank tab passes this by default)."""

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

    preset_tag = None
    if "--tag" in arguments:
        tag_index = arguments.index("--tag")
        if tag_index + 1 < len(arguments):
            preset_tag = arguments[tag_index + 1]

    use_similarity = "--no-similarity" not in arguments

    main(mode, port=port, preset_tag=preset_tag, use_similarity=use_similarity)
