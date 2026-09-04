# Contract: per-file rating details for Undertow's embedded comparer

Undertow's in-tab TagRank comparer (`undertow/templates/partials/girly/tagrank_comparer.html`,
driven by `undertow/webui.py`'s `/tagrank/compare/*` routes) needs to show, for each side of a
pair being judged: that picture's TrueSkill score, its rarest badge, and per-tag badge counts —
the same data the native PySide6 window computes in-process via `rating.py`/`badges.py`. Undertow
calls TagRank only over the HTTP API (see `docs/api.md`), so none of that is reachable today.
The client-side code (`undertow/tagrank_client.py::get_file_rating_details`) and the webui route
wiring are already built against the contract below and will 404/error until this exists —
implement this endpoint to light that plumbing up, no Undertow-side changes needed after.

## New endpoint

`GET /files/{file_id}/rating-details?hash=<file_hash>`

`file_id` is the Hydrus file id (path param, unused server-side beyond routing symmetry with the
rest of the API — TagRank has no file-id-keyed state). `hash` is the Hydrus file hash, required —
it's the `entity_id` badges.py and rating.py key pictures by. Tags are passed as a repeated query
param `tag` (one per visible tag on the file), since TagRank doesn't otherwise know what a given
file's tags are without a fresh Hydrus round trip — Undertow already has them from its own
`hydrus_client.get_file_metadata` call and can pass them straight through:

`GET /files/{file_id}/rating-details?hash=<file_hash>&tag=character%3Afoo&tag=outdoors&tag=...`

### Response

```json
{
  "photo_score": 42.7,
  "photo_confidence": 63.0,
  "picture_badge": {
    "id": "iron_will",
    "name": "Iron Will",
    "icon": "🛡️",
    "difficulty": "rare"
  },
  "picture_badges": [
    {"id": "iron_will", "name": "Iron Will", "icon": "🛡️", "difficulty": "rare"},
    {"id": "first_blood", "name": "First Blood", "icon": "🩸", "difficulty": "common"}
  ],
  "avg_tag_score": 33.75,
  "total_score": 76.45,
  "tags": [
    {"tag": "character:foo", "score": 55.1, "confidence": 80.0, "badge_count": 2, "badges": [
      {"id": "streak_5", "name": "On a Roll", "icon": "🔥", "difficulty": "common"},
      {"id": "top_dog", "name": "Top Dog", "icon": "🐕", "difficulty": "epic"}
    ]},
    {"tag": "outdoors", "score": 12.4, "confidence": 30.0, "badge_count": 0, "badges": []}
  ]
}
```

- `photo_score`: `rating.trueskill_number_from_rating(picture_rating)`, i.e. the same
  `(mu - 3*sigma) * MMR_SCALE` used everywhere else (`rating.py:38-39`) — the file's own picture
  rating, looked up by hash. `null` if the picture has no rating yet (never compared).
- `photo_confidence`: `rating.trueskill_confidence_from_rating(picture_rating)` (`rating.py:42-50`),
  0-100. `null` under the same condition as `photo_score`.
- `picture_badge`: the result of `badges.rarest_badge_id("picture", hash)` (`badges.py:234`)
  resolved to its `BadgeDef` (`id`, `name`, `icon`, `difficulty`) — the single rarest badge this
  picture holds, same selection the native window's `_make_badge_pill_for` uses (`ui/window.py:394`).
  `null` if the picture holds no badges. Kept for backward compat with the single-pill display.
- `picture_badges`: **every** badge this picture currently holds (not just the rarest), each
  resolved to its `BadgeDef` the same way as `picture_badge` — sorted highest-difficulty first,
  then alphabetically by name. Empty list if the picture holds no badges. This is what Undertow's
  image-corner overlay actually renders (one icon per held badge), since a single rarest-badge
  pill under-represents a picture that's earned several.
- `avg_tag_score`: the mean of every tag's `score` in the `tags` array below (skipping tags with
  a `null` score, i.e. never-rated tags) — `null` if every tag on the file is unrated.
- `total_score`: `photo_score + avg_tag_score` when both are available; falls back to whichever
  one is non-null if only one is; `null` if both are null. The single "combined MMR" number
  Undertow shows next to the comparer gauge.
- `tags`: one entry per tag passed in the `tag` query params, in the same order given. `score`/
  `confidence` are that tag's own `trueskill_number_from_rating`/`trueskill_confidence_from_rating`
  off its tag rating (`null`/`null` if the tag has never been rated — same as an unrated picture).
  `badge_count` is `len(badges.held_badge_ids("tag", tag))` (`badges.py:113`) — total badges held.
  `badges` is that same set of held badges resolved to full `BadgeDef`s (same sort order as
  `picture_badges`) — what Undertow's tag-pill hover tooltip lists by name/icon instead of just
  a bare count.

### Errors

Same envelope as the rest of the API (`{"detail": {"message": ...}}`, see `docs/api.md`) on a
malformed request (missing `hash`). An unknown `file_id`/`hash` is not an error — TagRank has no
picture-existence check of its own (it never touches Hydrus for this route), so an unrated hash
just yields `photo_score: null, photo_confidence: null, picture_badge: null` and a `tags` array
of all-null-score entries, exactly like a hash that happens to be genuinely unrated.

## Why this shape

- One route instead of separate `/rating`, `/badges/picture`, `/badges/tags` calls, since
  Undertow always wants all three together (one call per side per pair shown, so two calls per
  pair) and every value is a cheap local lookup (in-memory `RatingSystem.current_ratings` +
  `badges.load_badges()`/`load_badge_stats()`) — no reason to round-trip four times.
- Tags passed by Undertow rather than looked up server-side: TagRank's API has no Hydrus
  credentials of its own for arbitrary file metadata lookups in this flow (the existing
  `/sessions/*` routes only ever hand back `file_id`/`hash`, never tags — see `server.py`'s
  session routes), and Undertow already has the tag list in hand from its own Hydrus API key.
  Round-tripping through Hydrus a second time from TagRank's side would be pure waste.
- No live "win probability" is being asked of TagRank here — Undertow computes that itself from
  `photo_score`/`photo_confidence`/tag scores across both sides (see
  `_tagrank_compare_win_probability` in `undertow/webui.py`), since no calibrated win-probability
  formula exists anywhere in this codebase today (`rating.build_prediction_entry`'s `confidence`
  is a different, offline-logging-only heuristic — not reused here).
