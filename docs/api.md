# TagRank API — integration guide

TagRank normally runs as a desktop GUI. This API lets another application (e.g. "Undertow")
drive the same rating-comparison logic headlessly: start a session, fetch file pairs, submit
which one wins, and read back tag/history/chart data — all over local HTTP.

If you just want to poke around, start the server and open **http://127.0.0.1:8420/docs** —
every route below is documented there too, with a "Try it out" button that sends real requests.

## Starting and stopping the server

```
python main.py --serve             # listens on http://127.0.0.1:8420
python main.py --serve --port 9000 # custom port
```

This is meant to be launched as a subprocess by whatever app wants to use it, not run as a
persistent background daemon. There's no authentication — don't expose the port beyond
localhost.

To stop it, prefer `POST /shutdown` over killing the process outright, so any active session
gets a chance to be ended (see below) before the process exits. If your session-ending step
already called `DELETE /sessions/{id}`, killing the process directly is also safe — all
persistence happens synchronously inside `DELETE /sessions/{id}`, not at shutdown.

## Concepts

- **Session**: one in-memory rating-comparison run over a pool of Hydrus files. You create one,
  loop through pairs judging which file is "better," then end it — ending is what actually
  saves your work to disk (`data/ratings.json`, `data/comparisons.json`). An ended or never-ended
  session's judgments are lost, so always end a session you started, even on error/cancel paths.
- **Pool**: the set of files a session compares. Built once, at session start, from a Hydrus
  search query — either one you supply, or a tag drawn from `/search-options`.
- Only starting a session is asynchronous (it does a live Hydrus similarity search that can take
  a few seconds to tens of seconds). Every other call is a normal, fast, synchronous request.

## Full session walkthrough (curl)

```bash
# 1. Start a session (async - kicks off pool building)
curl -X POST http://127.0.0.1:8420/sessions \
     -H "Content-Type: application/json" \
     -d '{"query": ["character:mario"], "pool_size": 50}'
# -> {"job_id": "...", "status": "pending", "session_id": null, "error": null}

# 2. Poll until ready
curl http://127.0.0.1:8420/sessions/<job_id>
# -> {"job_id": "...", "status": "ready", "session_id": "<session_id>", "error": null}
# (status can also be "error", with a message in `error`)

# 3. Get a pair to show the user
curl http://127.0.0.1:8420/sessions/<session_id>/next-pair
# -> {"left": {"file_id": 123, "hash": "..."}, "right": {"file_id": 456, "hash": "..."}, "done": false}

# 4. Submit which side won
curl -X POST http://127.0.0.1:8420/sessions/<session_id>/result \
     -H "Content-Type: application/json" -d '{"choice": "left"}'

# 5. Repeat 3-4 for as many comparisons as you want. Optionally undo the last one:
curl -X POST http://127.0.0.1:8420/sessions/<session_id>/undo

# 6. End the session - THIS IS WHAT PERSISTS RESULTS TO DISK
curl -X DELETE http://127.0.0.1:8420/sessions/<session_id>
```

`GET .../next-pair` returns `"done": true` with null `left`/`right` when the pool is
exhausted (fewer than 2 un-compared files remain) — treat that as "nothing left to judge,"
not an error.

## Fetching an actual file to display

The API returns Hydrus `file_id`/`hash` for each side of a pair, not image bytes — fetch the
image the same way any Hydrus client does, via Hydrus's own Client API
(`GET /get_files/file?hash=<hash>` on your configured `API_URL`), using the same access key
TagRank itself is configured with (`config/KEYS`).

## Other data endpoints (no session required)

| Route | Purpose |
|---|---|
| `GET /tags` | Every rated tag and its current TrueSkill score. |
| `GET /search-options` | The Top/Random/Bottom tag picker the CLI shows at startup — use a returned `tag` as one element of `query` in `POST /sessions`. |
| `POST /search-options/filtered` | Same Top/Random/Bottom shape as `GET /search-options`, but each candidate tag is first narrowed by a fresh Hydrus search over score/resolution/rating-count/date-added/namespace/archive/service-key filters. See below. |
| `GET /history/predictions` | Raw per-comparison prediction-tracking records (one per judged pair, ever) — the same data the summary charts are built from, if you want to chart it yourself. |
| `GET /history/graphs` | The four summary charts (rolling accuracy, ratings-per-date, calibration, tag rankings) as `{"title", "png_base64"}`. |
| `GET /history/graphs/{index}.png` | One chart as a raw PNG, for direct `<img src="http://127.0.0.1:8420/history/graphs/0.png">` embedding without base64. |

## Error model

Every failure returns a non-2xx response with a JSON body:

```json
{"detail": {"error": "SessionNotFoundError", "message": "No active session with id '...'"}}
```

| `error` value | HTTP status | Meaning |
|---|---|---|
| `SessionNotFoundError` | 404 | The session id doesn't exist (never created, or already ended). |
| `NoPairAvailableError` | 409 | Called `/result` before calling `/next-pair`, or after the pool was already exhausted. |
| `JobNotFoundError` | 404 | The session-start job id doesn't exist. |
| `UnknownServiceKeyError` | 400 | `POST /search-options/filtered` was given a `file_service_keys`/`tag_service_keys` entry that doesn't match any key `GET /get_services` (on your Hydrus client) currently reports. |
| anything else (`HydrusConnectionError`, `HydrusPermissionError`, `NoRelevantFilesError`, ...) | 502 | Something about the Hydrus connection, permissions, or query went wrong — `message` has details. Also surfaces as `status: "error"` when it happens during session start; check `GET /sessions/{job_id}`. |

## `POST /search-options/filtered`

DB Search variant of `GET /search-options`. Same TrueSkill-ranked Top/Random/Bottom tag
picker, but each candidate tag is first narrowed by a fresh Hydrus search combining the tag
with every filter axis below — needed because none of these axes (aside from score) are
available on an already-fetched tag pill.

Request body (every field optional; a missing key behaves like a wide-open "no filter" range
on that axis):

```json
{
  "filter_tag": "",
  "min_files": 0,
  "score_min": -2.0, "score_max": 2.0,
  "aspect_ratio_min": 0.5, "aspect_ratio_max": 1.5,
  "pixel_count_min": 1800000, "pixel_count_max": 2200000,
  "rating_count_min": 0, "rating_count_max": 50,
  "date_added_days_ago_min": 150, "date_added_days_ago_max": 210,
  "namespace_mode": "all",
  "archive_mode": "all",
  "file_service_keys": null,
  "tag_service_keys": null
}
```

- `filter_tag` — substring match on tag text (case-insensitive), `""` = no filter.
- `min_files` — minimum matching-file count (after every other filter) for a tag to be
  included at all.
- `score_min`/`score_max` — TrueSkill score (`mu - 3*sigma`) band, same metric `GET /tags`
  reports.
- `aspect_ratio` is `width / height` (`1.0` = square, `>1` = wider than tall); `pixel_count`
  is `width * height`. Hydrus has no native predicate for either, so files matching the tag
  are fetched and filtered on these in Python.
- `rating_count` — Hydrus has no per-file "rating count" concept comparable across rating
  service types (like/dislike ratings are boolean, numerical ratings are a single star value),
  so this counts how many tags (across all tag services) currently sit on the file, as a
  stand-in metric.
- `date_added_days_ago_min`/`max` count backward from now — `min=150, max=210` means "added
  between 150 and 210 days ago." Converted to Hydrus's `system:time imported since/before N
  days ago` predicates.
- `namespace_mode` — `all` | `namespaced` (`system:has namespace`) | `unnamespaced`
  (`system:no namespace`).
- `archive_mode` — `all` | `archived` (`system:archived`) | `inbox` (`system:inbox`).
- `file_service_keys`/`tag_service_keys` — `null` or `[]` means "all services" (today's
  default); otherwise a list of keys matching what `GET /get_services` (on your Hydrus client)
  returns. An unrecognized key raises `UnknownServiceKeyError` (see the error table above).

Response body is identical in shape to `GET /search-options`:

```json
{
  "top": [{"index": 0, "tag": "character:mario", "score": 1.82, "file_count": 340}, ...],
  "random": [...],
  "bottom": [...]
}
```

An empty result after filtering is a normal `200` with empty `top`/`random`/`bottom` arrays,
not an error — only genuinely broken input (unknown service key) or a Hydrus-side failure
raises.

## Full route reference

See `/docs` (Swagger UI, interactive) or `/redoc` on a running server for the generated
reference with request/response schemas — it's kept in sync with the code automatically.
