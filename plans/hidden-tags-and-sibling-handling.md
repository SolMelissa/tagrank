# Contract: hidden-tags marker file and sibling-aware tag resolution

TagRank needed three fixes for tag filtering and performance: (1) exclude tags the user marks
as hidden in a Hydrus marker file, (2) exclude tags that have siblings except preserving
namespaced tags which should survive sibling collapse, and (3) add an opt-in speed mode for
namespaced-only tag universes.

This document describes the implementation and the feature contracts, not design rationale —
see the plan file for architectural context.

## 1. Hidden-tags marker file feature

Since Hydrus's Client API exposes no endpoint for the "tags hidden from search page" setting,
TagRank uses a dedicated **marker file** in Hydrus: the user tags it (via the normal "manage
tags" dialog) with every tag they want TagRank to ignore. TagRank fetches that file's tags
once at startup (and on explicit refresh) via `get_file_metadata` and treats those strings as
the hidden set.

### Configuration

- **New KEYS setting**: `TAGRANK_HIDDEN_TAGS_FILE_HASH` — the SHA256 hex hash of the marker file
  (format: `ABC123...xyz`, 64 chars). Leave blank or `FILL_ME_IN` to disable this feature.
  Get the hash from Hydrus: right-click the file → Share → Copy Hash (SHA256).
- **New module**: `tagrank/hidden_tags.py` — `refresh_hidden_tags(client)` fetches the marker
  file, `is_hidden_tag(tag)` checks membership (thread-safe).
- **New helper module**: `tagrank/setup_hidden_tags_marker.py` — CLI tool that creates a 1x1
  placeholder PNG, imports it into Hydrus, sets an explanatory note, and auto-saves the hash
  to config/KEYS. Run once during setup: `python -m tagrank.setup_hidden_tags_marker`.
- **New config function**: `config.is_excluded_tag(tag)` — combines `is_filtered_tag` (TAG_FILTERS)
  and `hidden_tags.is_hidden_tag` into one predicate for backward compatibility.

### Behavior

- **Startup**: `server.py`'s `_build_tag_index_on_startup` calls `hidden_tags.refresh_hidden_tags`
  before building the index, so hidden tags never enter the cache.
- **Caching**: `tag_index._to_file_record` filters hidden tags out when storing tags in the
  `FileRecord`, so the on-disk cache never contains them — they're not in `tags_by_service`.
- **Rating**: `rating.tags_from_file` and `tag_index._build_index`'s candidate-tag computation
  both skip hidden tags.
- **Searches**: `tag_index._file_record_passes_filters` doesn't see hidden tags because they
  never made it into the cache.
- **Errors & fallback**: If the marker file hash is unset, missing, or unreachable (Hydrus
  offline, file deleted), `refresh_hidden_tags` logs a warning and returns an empty set — TagRank
  continues running, treating it as "feature inactive" rather than crashing.

### Schema implications

The marker file can live in any Hydrus file service and tag service; it has no service-key
config, since its tags are merged across all services (same merge behavior as the main file
cache already does in `_to_file_record`).

## 2. Sibling handling and namespaced-tag carve-out

Hydrus's API returns `display_tags` (sibling-collapsed, "display" form) and `storage_tags`
(raw, pre-sibling form) in the same `get_file_metadata` response. TagRank now combines them:
take all `display_tags` (which Hydrus has already collapsed to ideal/sibling-resolved form),
then *add* any namespaced tags from `storage_tags` that aren't already in the display set
(preserving namespaced-tag pairs even when one side is a sibling).

### Shared utilities

- **New module**: `tagrank/tag_utils.py`
  - `has_namespace(tag)` — returns `True` if tag has a non-empty namespace prefix (e.g.
    `"performer:alice"`, `"rating:safe"` → `True`; `"outdoors"`, `":invalid"` → `False`).
  - `resolve_tags(service_data)` — takes one service's tag dict from file metadata, returns
    the final tag set (display ∪ namespaced-storage, excluding hidden/filtered).

### Behavior

- **File tagging**: When caching a file's tags (`tag_index._to_file_record`), use `resolve_tags`
  instead of just reading `display_tags["0"]` — this pulls in namespaced tags from storage that
  would otherwise be collapsed away.
- **Rating extraction**: `rating.tags_from_file` uses the same `resolve_tags` so files' tags
  are consistently resolved across both the cache and one-off file evaluations.
- **Sibling reconciliation**: When `load_ratings()` loads existing ratings from `ratings.json`,
  it calls `client.get_siblings_and_parents(unnamespaced_tags_only)` once and migrates any
  rating keyed by a now-obsolete non-ideal unnamespaced tag to its current ideal tag. If both
  the old and new keys already have ratings, it keeps the higher-confidence one (lower sigma).
  Namespaced tags are never migrated — they survive as-is even if they have siblings, per the
  carve-out rule.
- **Cost**: Both `display_tags` and `storage_tags` are already in the Hydrus API response, so
  zero extra round trips. Sibling reconciliation adds one `get_siblings_and_parents` call on
  startup, covering all unnamespaced ratings at once (batched) — very cheap.

### Error handling

Sibling reconciliation is best-effort: if the call fails, it logs a warning and proceeds with
ratings as-is (stale keys just don't migrate). This prevents a startup failure if Hydrus is
temporarily unreachable.

## 3. Namespace-only candidate mode

An opt-in `SETTINGS` option `TAG_UNIVERSE = all | namespaced_only` filters candidate tags
to namespaced-only when enabled. This shrinks the `candidate_tags` set used in
`tag_index._build_index`, reducing OR-search batch rounds and potentially the file set they match.

### Configuration

- **New SETTINGS key**: `TAG_UNIVERSE` — valid values: `all` (default) or `namespaced_only`.

### Behavior

- **Index build**: When `TAG_UNIVERSE = namespaced_only`, `_build_index` additionally filters
  `candidate_tags` using `has_namespace`, keeping only tags with namespace prefixes.
- **Logging**: The filter decision is logged at the same level as other candidate-tag counts,
  so toggling modes shows the before/after tag and file counts.
- **No forced migration**: Existing ratings stay as-is; this only affects the set of tags
  considered for new comparisons. An unnamespaced tag with a rating just won't appear in the
  tag picker when this mode is on.

### Measurement

No built-in speed measurement — the user can compare build times manually (watch the startup
logs when switching modes) using the existing logger output for total tags, files, and wall-clock
times already present in `_build_index`.

## API surface

No new HTTP endpoints or schema changes. All three features operate inside TagRank's existing
initialization pipeline.

## Testing

- **Hidden tags**: Create a marker file, tag it with 2-3 tags also on other files, verify those
  tags don't appear in `build_search_options`, aren't in the cache JSON, and don't affect ratings.
- **Siblings**: Set up a real sibling pair where one side is namespaced (e.g. `building:dwelling`
  → `dwelling:home`) and verify both appear in a test file's resolved tags. Then create an
  unnamespaced sibling pair (e.g. `house` → `dwelling:home`) and verify only the ideal form
  appears.
- **Namespace-only**: Switch to `TAG_UNIVERSE = namespaced_only`, force a build, watch the log
  for the reduced candidate-tag count and compare build times.
