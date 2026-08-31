#!/usr/bin/env python3
"""TagRank entrypoint. Run with `python main.py` (rank tags) or
`python main.py --create_image_ranking` (build the sort order)."""

import sys

from tagrank.app import MODE_CREATE_IMAGE_RANKING, MODE_RANK_TAGS, main

if __name__ == "__main__":
    arguments = sys.argv
    mode = MODE_CREATE_IMAGE_RANKING if "--create_image_ranking" in arguments else MODE_RANK_TAGS
    main(mode)
