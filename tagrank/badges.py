"""Badge tracking and evaluation.

Badges are permanent, entity-scoped achievements for tags and pictures (see the
"Fun Features" plan). Two hard rules, enforced structurally rather than by convention:

  1. Permanent: once earned, a badge is never revoked. record_result() only ever adds
     entries to data/badges.json, never removes them.
  2. Entity-scoped: a tag's badge conditions only ever read that tag's own EntityStats;
     a picture's badge conditions only ever read that picture's own EntityStats. There is
     no shared "comparison event" object passed to both, so a picture-side upset can never
     accidentally also earn a tag badge (or vice versa) just because they happened in the
     same comparison - the caller must call record_result() once per entity, separately.

Every badge has two icon forms:
  - `icon`: a Unicode emoji, used wherever plain text is shown (toasts, tooltips, the
    preset info box) with no image loading involved.
  - `icon_file`: a real SVG icon from the game-icons.net set (CC BY 3.0 - see
    tagrank/ui/assets/badges/LICENSE.txt), used for the actual on-screen badge display next
    to tags and pictures. One file per badge, so all 60 icons are visually distinct.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Literal

from config import DATA_DIR

ASSETS_DIR = Path(__file__).resolve().parent / "ui" / "assets" / "badges"


def icon_path(badge_id: str) -> Path:
    """Full path to a badge's SVG icon file."""
    return ASSETS_DIR / f"{badge_id}.svg"

EntityType = Literal["tag", "picture"]

STATS_PATH = DATA_DIR / "badge_stats.json"
BADGES_PATH = DATA_DIR / "badges.json"

# A win/loss result counts as a confidence-threshold-worthy upset once the mu gap between
# winner and loser is at least this many multiples of the loser's sigma. Deliberately high
# (3x) so "Giant Slayer"/"Dark Horse" stay rare, per-design ask that these be hard to earn.
UPSET_SIGMA_MULTIPLE = 3.0


# --------------------------------------------------------------------------------------
# Per-entity running stats (data/badge_stats.json). Updated incrementally at the moment
# of each comparison result, since Hydrus/TagRank's existing logs don't retain enough
# per-tag/per-picture history to reconstruct streaks after the fact.
# --------------------------------------------------------------------------------------

@dataclass
class EntityStats:
    wins: int = 0
    losses: int = 0
    total: int = 0
    streak: int = 0                     # >0: current win streak length; <0: current loss streak length
    prev_streak: int = 0                # value of `streak` right before the current run started
    best_win_streak: int = 0
    last20: list[bool] = field(default_factory=list)  # True=win, most-recent last; capped at 20
    first_ts: float | None = None
    baseline_mu: float | None = None    # mu snapshot once `total` first reaches 20 (Overachiever)
    confidence_since: float | None = None  # timestamp sigma first dropped below the confidence threshold, held continuously
    tournament_wins: int = 0
    tournament_finals: int = 0          # times reached a tournament final (win or lose)
    rediscovery_wins: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EntityStats":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_stats() -> dict[str, dict[str, dict]]:
    raw = _load_json(STATS_PATH, {"tags": {}, "pictures": {}})
    raw.setdefault("tags", {})
    raw.setdefault("pictures", {})
    return raw


def load_badges() -> dict[str, dict[str, list[dict]]]:
    raw = _load_json(BADGES_PATH, {"tags": {}, "pictures": {}})
    raw.setdefault("tags", {})
    raw.setdefault("pictures", {})
    return raw


def held_badge_ids(entity_type: EntityType, entity_id: str) -> set[str]:
    badges = load_badges()
    bucket = badges[entity_type + "s"]
    return {entry["badge_id"] for entry in bucket.get(entity_id, [])}


# --------------------------------------------------------------------------------------
# Badge catalogue
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BadgeDef:
    id: str
    name: str
    icon: str
    entity_type: EntityType
    description: str
    condition: Callable[["BadgeContext"], bool]


@dataclass
class BadgeContext:
    """Everything a condition function may read - strictly scoped to one entity."""
    stats: EntityStats
    mu: float
    sigma: float
    confidence_threshold: float
    held: set[str]
    rank_pct: float | None = None          # 1.0 = best in its pool, 0.0 = worst; None if unknown
    upset_sigma_multiple: float | None = None  # only set on the comparison where this entity won an upset
    beat_top3: bool = False                # only set on the comparison where this entity beat a top-3 opponent


def _confident(ctx: BadgeContext) -> bool:
    return ctx.sigma < ctx.confidence_threshold


TAG_BADGES: list[BadgeDef] = [
    BadgeDef("first_blood", "First Blood", "🩸", "tag", "First-ever win", lambda c: c.stats.wins >= 1),
    BadgeDef("on_a_roll", "On a Roll", "🔥", "tag", "5-win streak", lambda c: c.stats.streak >= 5),
    BadgeDef("unstoppable", "Unstoppable", "🌪️", "tag", "10-win streak", lambda c: c.stats.streak >= 10),
    BadgeDef("iron_tag", "Iron Tag", "🛡️", "tag", "50 total comparisons survived", lambda c: c.stats.total >= 50),
    BadgeDef("crowd_favorite", "Crowd Favorite", "⭐", "tag", "mu crosses a high absolute threshold", lambda c: c.mu >= 35.0),
    BadgeDef("rock_solid", "Rock Solid", "🪨", "tag", "sigma drops below the confidence threshold", _confident),
    BadgeDef("giant_slayer", "Giant Slayer", "⚔️", "tag", "Beat a tag rated 2+ tiers higher", lambda c: (c.upset_sigma_multiple or 0) >= UPSET_SIGMA_MULTIPLE),
    BadgeDef("comeback_kid", "Comeback Kid", "🔁", "tag", "3-loss streak recovered into a 3-win streak", lambda c: c.stats.prev_streak <= -3 and c.stats.streak >= 3),
    BadgeDef("century_club", "Century Club", "💯", "tag", "100 total comparisons", lambda c: c.stats.total >= 100),
    BadgeDef("undefeated_debut", "Undefeated Debut", "🎬", "tag", "Wins its first 5 comparisons in a row", lambda c: c.stats.total >= 5 and c.stats.losses == 0 and c.stats.streak >= 5),
    BadgeDef("marathoner", "Marathoner", "🏃", "tag", "200 total comparisons", lambda c: c.stats.total >= 200),
    BadgeDef("untouchable", "Untouchable", "🧊", "tag", "15-win streak", lambda c: c.stats.streak >= 15),
    BadgeDef("legendary", "Legendary", "🐉", "tag", "20-win streak", lambda c: c.stats.streak >= 20),
    BadgeDef("top_dog", "Top Dog", "🐕", "tag", "Reaches #1 mu rank in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.999),
    BadgeDef("elite_eight", "Elite Eight", "8️⃣", "tag", "Reaches top 8 by mu", lambda c: c.rank_pct is not None and c.rank_pct >= 0.9),
    BadgeDef("sharpshooter", "Sharpshooter", "🎯", "tag", "80%+ win rate over its last 20 comparisons", lambda c: len(c.stats.last20) >= 20 and sum(c.stats.last20) / len(c.stats.last20) >= 0.8),
    BadgeDef("slow_burn", "Slow Burn", "🕯️", "tag", "Reaches Rock Solid confidence only after 150+ comparisons", lambda c: _confident(c) and c.stats.total >= 150),
    BadgeDef("fast_riser", "Fast Riser", "🚀", "tag", "Reaches Rock Solid confidence in under 30 comparisons", lambda c: _confident(c) and c.stats.total < 30),
    BadgeDef("redemption_arc", "Redemption Arc", "🌅", "tag", "5-loss streak recovered into a 5-win streak", lambda c: c.stats.prev_streak <= -5 and c.stats.streak >= 5),
    BadgeDef("battle_scarred", "Battle Scarred", "🩹", "tag", "Survives 20 losses, still net-positive record", lambda c: c.stats.losses >= 20 and c.stats.wins > c.stats.losses),
    BadgeDef("overachiever", "Overachiever", "📈", "tag", "mu exceeds its own first-20-comparison baseline by a large margin", lambda c: c.stats.baseline_mu is not None and c.mu - c.stats.baseline_mu >= 10.0),
    BadgeDef("consistency_king", "Consistency King", "👑", "tag", "sigma stays below the confidence threshold continuously across 100+ comparisons", lambda c: _confident(c) and c.stats.confidence_since is not None and c.stats.total >= 100),
    BadgeDef("giant_slayer_ii", "Giant Slayer II", "🗡️", "tag", "Beats a top-3 ranked tag at least once", lambda c: c.beat_top3),
    BadgeDef("ironclad", "Ironclad", "⛓️", "tag", "Never drops below a positive record after its first 20 comparisons", lambda c: c.stats.total >= 20 and c.stats.wins > c.stats.losses),
    BadgeDef("dark_horse_tag", "Dark Horse Tag", "🐴", "tag", "Starts with a loss but finishes in the top 10", lambda c: c.stats.total >= 1 and not c.stats.last20[:1] == [True] and c.rank_pct is not None and c.rank_pct >= 0.8),
    BadgeDef("streak_breaker", "Streak Breaker", "✂️", "tag", "Ends another tag's active 5+ win streak", lambda c: (c.upset_sigma_multiple or 0) > 0 and c.stats.streak >= 1),
    BadgeDef("halfway_hero", "Halfway Hero", "🏁", "tag", "Reaches 50 comparisons with a winning record", lambda c: c.stats.total >= 50 and c.stats.wins > c.stats.losses),
    BadgeDef("grand_champion", "Grand Champion", "🏆", "tag", "Its picture wins a Tournament bracket while this tag was the seed theme", lambda c: False),  # awarded manually, see award_manual
    BadgeDef("precision", "Precision", "🧭", "tag", "sigma below threshold and mu in the top 10% at the same time", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.9),
    BadgeDef("hall_of_fame", "Hall of Fame", "🏛️", "tag", "Already holds Century Club + Rock Solid + Top Dog", lambda c: {"century_club", "rock_solid", "top_dog"} <= c.held),
]

PICTURE_BADGES: list[BadgeDef] = [
    BadgeDef("fan_favorite", "Fan Favorite", "💖", "picture", "First to reach top-10 mu in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.8),
    BadgeDef("photogenic", "Photogenic", "📸", "picture", "10-win streak", lambda c: c.stats.streak >= 10),
    BadgeDef("veteran", "Veteran", "🎖️", "picture", "Survives 100 comparisons", lambda c: c.stats.total >= 100),
    BadgeDef("dark_horse", "Dark Horse", "🐎", "picture", "Beats a much higher-rated picture", lambda c: (c.upset_sigma_multiple or 0) >= UPSET_SIGMA_MULTIPLE),
    BadgeDef("consistent", "Consistent", "⚖️", "picture", "sigma below confidence threshold with mu in top quartile", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.75),
    BadgeDef("tournament_champion", "Tournament Champion", "🏅", "picture", "Wins a full Tournament bracket", lambda c: c.stats.tournament_wins >= 1),
    BadgeDef("rediscovered_gem", "Rediscovered Gem", "💎", "picture", "Resurfaced via Random Rediscovery and wins", lambda c: c.stats.rediscovery_wins >= 1),
    BadgeDef("perfect_streak", "Perfect Streak", "🌟", "picture", "10 straight wins, no losses ever", lambda c: c.stats.streak >= 10 and c.stats.losses == 0),
    BadgeDef("crowd_pleaser", "Crowd Pleaser", "🙌", "picture", "High mu reached with low total comparisons", lambda c: c.mu >= 30.0 and c.stats.total <= 15),
    BadgeDef("old_reliable", "Old Reliable", "🕰️", "picture", "First rated, still top-10 after N comparisons", lambda c: c.stats.total >= 50 and c.rank_pct is not None and c.rank_pct >= 0.8),
    BadgeDef("marathon_runner", "Marathon Runner", "🥾", "picture", "200 total comparisons", lambda c: c.stats.total >= 200),
    BadgeDef("untouchable_pic", "Untouchable", "❄️", "picture", "15-win streak", lambda c: c.stats.streak >= 15),
    BadgeDef("legendary_pic", "Legendary", "🦄", "picture", "20-win streak", lambda c: c.stats.streak >= 20),
    BadgeDef("top_of_the_pile", "Top of the Pile", "🗻", "picture", "Reaches #1 mu in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.999),
    BadgeDef("elite_eight_pic", "Elite Eight", "🎱", "picture", "Top 8 by mu", lambda c: c.rank_pct is not None and c.rank_pct >= 0.9),
    BadgeDef("sharpshooter_pic", "Sharpshooter", "🏹", "picture", "80%+ win rate over its last 20 comparisons", lambda c: len(c.stats.last20) >= 20 and sum(c.stats.last20) / len(c.stats.last20) >= 0.8),
    BadgeDef("slow_burn_pic", "Slow Burn", "🐢", "picture", "Reaches confidence threshold only after 150+ comparisons", lambda c: _confident(c) and c.stats.total >= 150),
    BadgeDef("fast_riser_pic", "Fast Riser", "🐇", "picture", "Reaches confidence threshold in under 30 comparisons", lambda c: _confident(c) and c.stats.total < 30),
    BadgeDef("redemption_arc_pic", "Redemption Arc", "🌄", "picture", "5-loss streak recovered into a 5-win streak", lambda c: c.stats.prev_streak <= -5 and c.stats.streak >= 5),
    BadgeDef("battle_scarred_pic", "Battle Scarred", "🧯", "picture", "Survives 20 losses, still net-positive record", lambda c: c.stats.losses >= 20 and c.stats.wins > c.stats.losses),
    BadgeDef("bracket_buster", "Bracket Buster", "💥", "picture", "Wins a tournament match as a low/random seed against a much higher-rated pic", lambda c: False),  # awarded manually
    BadgeDef("finalist", "Finalist", "🥈", "picture", "Reaches a tournament final without winning it", lambda c: c.stats.tournament_finals >= 1 and c.stats.tournament_wins == 0),
    BadgeDef("double_champion", "Double Champion", "🥇", "picture", "Wins two separate Tournament brackets", lambda c: c.stats.tournament_wins >= 2),
    BadgeDef("ironclad_pic", "Ironclad", "🔒", "picture", "Never drops below a positive record after its first 20 comparisons", lambda c: c.stats.total >= 20 and c.stats.wins > c.stats.losses),
    BadgeDef("late_bloomer", "Late Bloomer", "🌸", "picture", "Starts with a loss but finishes in the top 10", lambda c: c.stats.total >= 1 and c.rank_pct is not None and c.rank_pct >= 0.8),
    BadgeDef("streak_breaker_pic", "Streak Breaker", "🔨", "picture", "Ends another picture's active 5+ win streak", lambda c: (c.upset_sigma_multiple or 0) > 0 and c.stats.streak >= 1),
    BadgeDef("halfway_hero_pic", "Halfway Hero", "🚩", "picture", "Reaches 50 comparisons with a winning record", lambda c: c.stats.total >= 50 and c.stats.wins > c.stats.losses),
    BadgeDef("rediscovered_twice", "Rediscovered Twice", "🔍", "picture", "Resurfaced by Random Rediscovery more than once and wins each time", lambda c: c.stats.rediscovery_wins >= 2),
    BadgeDef("precision_pic", "Precision", "🧿", "picture", "sigma below threshold and mu in the top 10% at the same time", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.9),
    BadgeDef("hall_of_fame_pic", "Hall of Fame", "🏟️", "picture", "Already holds Veteran + Consistent + Top of the Pile", lambda c: {"veteran", "consistent", "top_of_the_pile"} <= c.held),
]

BADGES: dict[EntityType, list[BadgeDef]] = {"tag": TAG_BADGES, "picture": PICTURE_BADGES}
BADGE_BY_ID: dict[str, BadgeDef] = {b.id: b for b in TAG_BADGES + PICTURE_BADGES}

assert len({b.icon for b in BADGE_BY_ID.values()}) == len(BADGE_BY_ID), "Badge emoji icons must be unique per-badge."
assert len({icon_path(bid) for bid in BADGE_BY_ID}) == len(BADGE_BY_ID), "Badge icon files must be unique per-badge."
_missing_icons = [bid for bid in BADGE_BY_ID if not icon_path(bid).is_file()]
assert not _missing_icons, f"Missing badge icon file(s) for: {_missing_icons}"


# --------------------------------------------------------------------------------------
# Recording results
# --------------------------------------------------------------------------------------

def _update_stats(stats: EntityStats, *, won: bool, mu: float, ts: float, threshold: float, sigma: float, is_rediscovery: bool) -> None:
    stats.total += 1
    if stats.first_ts is None:
        stats.first_ts = ts
    if won:
        stats.wins += 1
        stats.prev_streak = stats.streak if stats.streak < 0 else stats.prev_streak
        stats.streak = stats.streak + 1 if stats.streak > 0 else 1
        stats.best_win_streak = max(stats.best_win_streak, stats.streak)
    else:
        stats.losses += 1
        stats.prev_streak = stats.streak if stats.streak > 0 else stats.prev_streak
        stats.streak = stats.streak - 1 if stats.streak < 0 else -1

    stats.last20.append(won)
    if len(stats.last20) > 20:
        stats.last20 = stats.last20[-20:]

    if stats.total == 20:
        stats.baseline_mu = mu

    if sigma < threshold:
        if stats.confidence_since is None:
            stats.confidence_since = ts
    else:
        stats.confidence_since = None

    if won and is_rediscovery:
        stats.rediscovery_wins += 1


def record_result(
    entity_type: EntityType,
    entity_id: str,
    *,
    won: bool,
    mu: float,
    sigma: float,
    confidence_threshold: float,
    ts: float | None = None,
    rank_pct: float | None = None,
    upset_sigma_multiple: float | None = None,
    beat_top3: bool = False,
    is_rediscovery: bool = False,
) -> list[BadgeDef]:
    """Update one entity's running stats and evaluate its (and only its) badge conditions.

    Returns the list of newly-earned badges (empty if none). Safe to call once per entity
    per comparison - never pass both winner and loser data into a single call.
    """
    ts = ts or time.time()
    stats_all = _load_stats()
    bucket = stats_all[entity_type + "s"]
    stats = EntityStats.from_dict(bucket.get(entity_id, {}))
    _update_stats(stats, won=won, mu=mu, ts=ts, threshold=confidence_threshold, sigma=sigma, is_rediscovery=is_rediscovery)
    bucket[entity_id] = asdict(stats)
    _save_json(STATS_PATH, stats_all)

    held = held_badge_ids(entity_type, entity_id)
    ctx = BadgeContext(
        stats=stats,
        mu=mu,
        sigma=sigma,
        confidence_threshold=confidence_threshold,
        held=held,
        rank_pct=rank_pct,
        upset_sigma_multiple=upset_sigma_multiple if won else None,
        beat_top3=beat_top3 if won else False,
    )
    newly: list[BadgeDef] = []
    for badge in BADGES[entity_type]:
        if badge.id in held:
            continue
        try:
            if badge.condition(ctx):
                newly.append(badge)
                held.add(badge.id)
        except Exception:
            # A badge condition failing to evaluate should never break rating/session flow.
            continue

    if newly:
        badges_all = load_badges()
        entry_list = badges_all[entity_type + "s"].setdefault(entity_id, [])
        for badge in newly:
            entry_list.append({"badge_id": badge.id, "earned_at": ts})
        _save_json(BADGES_PATH, badges_all)

    return newly


def award_manual(entity_type: EntityType, entity_id: str, badge_id: str, ts: float | None = None) -> bool:
    """Award an event-driven badge directly (tournament wins/finals, bracket upsets) instead
    of through a stats condition. Returns False if already held (permanent, so a no-op)."""
    held = held_badge_ids(entity_type, entity_id)
    if badge_id in held:
        return False
    ts = ts or time.time()
    badges_all = load_badges()
    entry_list = badges_all[entity_type + "s"].setdefault(entity_id, [])
    entry_list.append({"badge_id": badge_id, "earned_at": ts})
    _save_json(BADGES_PATH, badges_all)
    return True


def bump_tournament_stat(entity_type: EntityType, entity_id: str, *, won_bracket: bool, reached_final: bool) -> None:
    """Increment tournament-related counters used by tournament_wins/finalist-style badges."""
    stats_all = _load_stats()
    bucket = stats_all[entity_type + "s"]
    stats = EntityStats.from_dict(bucket.get(entity_id, {}))
    if won_bracket:
        stats.tournament_wins += 1
    if reached_final:
        stats.tournament_finals += 1
    bucket[entity_id] = asdict(stats)
    _save_json(STATS_PATH, stats_all)
