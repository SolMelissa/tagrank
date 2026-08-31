# Fun Features

Seven gameplay/UX features layered on top of TagRank's core TrueSkill ranking, plus a
settings panel to control them. All are available from both the desktop GUI
(`tagrank/ui/window.py`) and the headless HTTP API (`tagrank/server.py`), which share one
underlying settings/session model (`tagrank/service.py`).

## 1. Tournament / Bracket Mode

Draws a random subset of the active pool (`MAX_TOURNAMENT_SIZE` in `config/SETTINGS`,
default 64) and runs a single-elimination bracket through the normal comparison flow.
Seeding is random, not top-N, so the bracket doesn't just pit already-favored files against
each other. Rounds down to the nearest power of 2 that fits within the cap.

- GUI: **Pool → Start Tournament Mode**, or the toolbar button.
- API: `POST /sessions/{id}/tournament`, `GET /sessions/{id}/tournament/next-match`,
  `POST /sessions/{id}/tournament/result`.
- Implementation: `tagrank/tournament.py`.

## 2. Badges

60 permanent, entity-scoped achievements (30 for tags, 30 for pictures) — see the full list
below. Once earned, a badge is never revoked.

**Entity-scoping rule** (enforced in code, not by convention): a tag's badge conditions only
ever read that tag's own stats, and a picture's only ever read that picture's own stats.
There is no shared "comparison event" passed to both checkers, so a picture-side upset can
never accidentally also earn a tag badge.

- Storage: `data/badge_stats.json` (running per-entity counters) and `data/badges.json`
  (earned badges + timestamps).
- Hydrus sync: **picture badges only** write back to Hydrus as real tags in a `badge:`
  namespace (e.g. `badge:photogenic`). Tag badges stay TagRank-internal — Hydrus has no
  "tag on a tag" concept.
- Icons: every badge has a real SVG icon (not just an emoji) from the
  [game-icons.net](https://game-icons.net) set (CC BY 3.0 — see
  `tagrank/ui/assets/badges/LICENSE.txt` for attribution), shown next to each tag/picture
  with a tooltip giving the badge's name and description. A Unicode emoji is kept per badge
  too, for plain-text contexts (toasts, the preset info box) where loading an image isn't
  worth it.
- GUI: badge icons render live under each compared picture; **View → View Badges...** lists
  everything earned so far.
- API: `GET /badges`.
- Implementation: `tagrank/badges.py`.

## 3. Rising Star Feed

A live leaderboard of the biggest tag mu movers so far in the current session, shown at the
top of the Summary Dashboard and recomputed after every comparison. Toggle via
`RISING_STAR_FEED_ENABLED` / the View menu.

## 4. Confidence Duel Mode + Divergence Mode

Two new pool pairing strategies alongside the existing Top/Random/Bottom:

- **Confidence Duel**: pairs whose sigma ranges overlap the most (the model is least sure
  who'd win) — converges rankings fastest.
- **Divergence**: pairs with close mu regardless of sigma, to force already-clustered/tied
  items to compete and separate.

Select via `POOL_STRATEGY` in `config/SETTINGS`, the GUI's Pool menu, or the `pool_strategy`
param on `POST /sessions`.

## 5. Underdog Alerts

Flags upsets — a lower-mu entity beating a much higher-mu one — with the mu-gap expressed as
a multiple of the loser's sigma. Two safeguards keep this meaningful:

- Only fires for entities whose sigma is already below `CONFIDENCE_SIGMA_THRESHOLD`
  (default 3.0) — early, volatile ratings would otherwise look like constant "upsets."
- The gap itself must be at least 3× the loser's sigma to count, matching the "Giant
  Slayer"/"Dark Horse" badge thresholds.

Toggle via `UNDERDOG_ALERTS_ENABLED`.

## 6. Mood/Theme Sessions (Presets)

Named presets bundling a pool-builder config, defined in `config/presets.json`:

- **Deep Dive** — narrow seed, large pool, tight similarity distance.
- **Wide Sweep** — broad/no seed, random strategy, loose similarity distance.
- **Cleanup Pass** — targets the least-confident (highest-sigma) items, paired via
  Confidence Duel to burn down the backlog fast.

GUI: **Search → Mood/Theme Presets**. API: `GET /presets`, `POST /sessions` with
`preset_id`.

## 7. Random Rediscovery

"Surprise Me" resurfaces a highly-rated but rarely-recently-compared item, paired against a
random opponent. Needs comparison timestamps, which `data/comparisons.json` entries now
carry (`[winner_id, loser_id, timestamp]`; older two-element entries still load fine).

- GUI: Pool menu / toolbar button.
- API: `GET /sessions/{id}/rediscover`.

## Settings Panel

`tagrank/settings.py`'s `Settings` stays a frozen, validated snapshot; `SettingsStore` holds
the current one and rebuilds-and-swaps the whole tree on an edit (never mutates a field in
place), persisting changed keys back to `config/SETTINGS`. The GUI menu bar/Settings dialog
and the headless API (`GET`/`PATCH /settings`) are two views of the same store.

The GUI's `Window` is now a `QMainWindow` (previously a bare `QWidget`) specifically to host
a real menu bar and toolbar. Frequent toggles (pool strategy, Rising Star, Underdog Alerts,
Tournament/Rediscover) live on the toolbar; the rest are in **Settings → Settings...**.
Hydrus connection keys are shown read-only — they're secrets/connection identity, not
session tuning, and changing them mid-session doesn't make sense without reconnecting.

## Full badge list

### Tags (30)
First Blood, On a Roll, Unstoppable, Iron Tag, Crowd Favorite, Rock Solid, Giant Slayer,
Comeback Kid, Century Club, Undefeated Debut, Marathoner, Untouchable, Legendary, Top Dog,
Elite Eight, Sharpshooter, Slow Burn, Fast Riser, Redemption Arc, Battle Scarred,
Overachiever, Consistency King, Giant Slayer II, Ironclad, Dark Horse Tag, Streak Breaker,
Halfway Hero, Grand Champion, Precision, Hall of Fame.

### Pictures (30)
Fan Favorite, Photogenic, Veteran, Dark Horse, Consistent, Tournament Champion, Rediscovered
Gem, Perfect Streak, Crowd Pleaser, Old Reliable, Marathon Runner, Untouchable, Legendary,
Top of the Pile, Elite Eight, Sharpshooter, Slow Burn, Fast Riser, Redemption Arc, Battle
Scarred, Bracket Buster, Finalist, Double Champion, Ironclad, Late Bloomer, Streak Breaker,
Halfway Hero, Rediscovered Twice, Precision, Hall of Fame.

Full conditions/thresholds for each: see `tagrank/badges.py`'s `TAG_BADGES` /
`PICTURE_BADGES` lists, or hover a badge icon in the GUI.

## Known limitations

- **Rank-percentile badges are session-scoped.** TagRank has no single global list of every
  picture's rating to rank against (only the active session's pool), so picture badges like
  "Top of the Pile" or "Elite Eight" are ranked within the current session's pool, not the
  whole Hydrus library. Tag rank badges use the full `data/ratings.json`, which does cover
  every rated tag.
- **`ComparisonFlowTests.test_full_pair_and_submit_flow_updates_ratings_and_writes_choices`**
  in `tests/test_service.py` fails in any environment with real `TAGRANK_MMR_SERVICE_KEY`/
  `TAGRANK_MMR_CONFIDENCE_SERVICE_KEY` values configured in `config/KEYS` — confirmed
  pre-existing on unmodified `main` with the same config, unrelated to this feature set.
  Not touched here (out of scope).
