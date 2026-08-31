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
| anything else (`HydrusConnectionError`, `HydrusPermissionError`, `NoRelevantFilesError`, ...) | 502 | Something about the Hydrus connection, permissions, or query went wrong — `message` has details. Also surfaces as `status: "error"` when it happens during session start; check `GET /sessions/{job_id}`. |

## Full route reference

See `/docs` (Swagger UI, interactive) or `/redoc` on a running server for the generated
reference with request/response schemas — it's kept in sync with the code automatically.
