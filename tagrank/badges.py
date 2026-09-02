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
Difficulty = Literal["common", "rare", "epic", "legendary"]

STATS_PATH = DATA_DIR / "badge_stats.json"
BADGES_PATH = DATA_DIR / "badges.json"

# A win/loss result counts as a confidence-threshold-worthy upset once the mu gap between
# winner and loser is at least this many multiples of the loser's sigma. Deliberately high
# (3x) so "Giant Slayer"/"Dark Horse" stay rare, per-design ask that these be hard to earn.
UPSET_SIGMA_MULTIPLE = 3.0

# Rank-percentile badges (Top Dog, Elite Eight, Precision, ...) are only meaningful once
# there's a real pool to rank against - with a handful of entities, winning once trivially
# puts you "in the top 20%", which is how badges ended up firing in bunches on every
# comparison. Require at least this many rated entities before rank_pct is considered.
MIN_POOL_FOR_RANK_BADGES = 20


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
    # Rarity tier, required on every entry (no default) so a new badge can't be added without
    # deliberately picking one. Roughly: common = trivially reachable early on (total>=1,
    # streak>=5, sigma just crossing the confidence threshold); rare = a modest sustained
    # effort (streak>=10-15, total>=20-50, 80% win rate); epic = a real grind or a hard
    # condition (total>=100-150, streak>=15-20, beating a top-3 opponent, a 3-sigma upset);
    # legendary = top of the scale / effectively best-in-pool (total>=200, streak>=20,
    # rank_pct>=0.999, "awarded manually", or stacking several other badges as a prereq).
    difficulty: Difficulty


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
    BadgeDef("first_blood", "First Blood", "🩸", "tag", "First-ever win", lambda c: c.stats.wins >= 1, difficulty="common"),
    BadgeDef("on_a_roll", "On a Roll", "🔥", "tag", "5-win streak", lambda c: c.stats.streak >= 5, difficulty="common"),
    BadgeDef("unstoppable", "Unstoppable", "🌪️", "tag", "10-win streak", lambda c: c.stats.streak >= 10, difficulty="rare"),
    BadgeDef("iron_tag", "Iron Tag", "🛡️", "tag", "50 total comparisons survived", lambda c: c.stats.total >= 50, difficulty="rare"),
    BadgeDef("crowd_favorite", "Crowd Favorite", "⭐", "tag", "mu crosses a high absolute threshold", lambda c: c.mu >= 35.0, difficulty="rare"),
    BadgeDef("rock_solid", "Rock Solid", "🪨", "tag", "sigma drops below the confidence threshold", _confident, difficulty="common"),
    BadgeDef("giant_slayer", "Giant Slayer", "⚔️", "tag", "Beat a tag rated 2+ tiers higher", lambda c: (c.upset_sigma_multiple or 0) >= UPSET_SIGMA_MULTIPLE, difficulty="epic"),
    BadgeDef("comeback_kid", "Comeback Kid", "🔁", "tag", "3-loss streak recovered into a 3-win streak", lambda c: c.stats.prev_streak <= -3 and c.stats.streak >= 3, difficulty="rare"),
    BadgeDef("century_club", "Century Club", "💯", "tag", "100 total comparisons", lambda c: c.stats.total >= 100, difficulty="epic"),
    BadgeDef("undefeated_debut", "Undefeated Debut", "🎬", "tag", "Wins its first 5 comparisons in a row", lambda c: c.stats.total >= 5 and c.stats.losses == 0 and c.stats.streak >= 5, difficulty="rare"),
    BadgeDef("marathoner", "Marathoner", "🏃", "tag", "200 total comparisons", lambda c: c.stats.total >= 200, difficulty="legendary"),
    BadgeDef("untouchable", "Untouchable", "🧊", "tag", "15-win streak", lambda c: c.stats.streak >= 15, difficulty="epic"),
    BadgeDef("legendary", "Legendary", "🐉", "tag", "20-win streak", lambda c: c.stats.streak >= 20, difficulty="legendary"),
    BadgeDef("top_dog", "Top Dog", "🐕", "tag", "Reaches #1 mu rank in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.999, difficulty="legendary"),
    BadgeDef("elite_eight", "Elite Eight", "8️⃣", "tag", "Reaches top 8 by mu", lambda c: c.rank_pct is not None and c.rank_pct >= 0.9, difficulty="epic"),
    BadgeDef("sharpshooter", "Sharpshooter", "🎯", "tag", "80%+ win rate over its last 20 comparisons", lambda c: len(c.stats.last20) >= 20 and sum(c.stats.last20) / len(c.stats.last20) >= 0.8, difficulty="rare"),
    BadgeDef("slow_burn", "Slow Burn", "🕯️", "tag", "Reaches Rock Solid confidence only after 150+ comparisons", lambda c: _confident(c) and c.stats.total >= 150, difficulty="epic"),
    BadgeDef("fast_riser", "Fast Riser", "🚀", "tag", "Reaches Rock Solid confidence in under 30 comparisons", lambda c: _confident(c) and c.stats.total < 30, difficulty="rare"),
    BadgeDef("redemption_arc", "Redemption Arc", "🌅", "tag", "5-loss streak recovered into a 5-win streak", lambda c: c.stats.prev_streak <= -5 and c.stats.streak >= 5, difficulty="epic"),
    BadgeDef("battle_scarred", "Battle Scarred", "🩹", "tag", "Survives 20 losses, still net-positive record", lambda c: c.stats.losses >= 20 and c.stats.wins > c.stats.losses, difficulty="epic"),
    BadgeDef("overachiever", "Overachiever", "📈", "tag", "mu exceeds its own first-20-comparison baseline by a large margin", lambda c: c.stats.baseline_mu is not None and c.mu - c.stats.baseline_mu >= 10.0, difficulty="epic"),
    BadgeDef("consistency_king", "Consistency King", "👑", "tag", "sigma stays below the confidence threshold continuously across 100+ comparisons", lambda c: _confident(c) and c.stats.confidence_since is not None and c.stats.total >= 100, difficulty="legendary"),
    BadgeDef("giant_slayer_ii", "Giant Slayer II", "🗡️", "tag", "Beats a top-3 ranked tag at least once", lambda c: c.beat_top3, difficulty="epic"),
    BadgeDef("ironclad", "Ironclad", "⛓️", "tag", "Never drops below a positive record after its first 20 comparisons", lambda c: c.stats.total >= 20 and c.stats.wins > c.stats.losses, difficulty="rare"),
    BadgeDef("dark_horse_tag", "Dark Horse Tag", "🐴", "tag", "Starts with a loss but finishes in the top 10", lambda c: c.stats.total >= 1 and not c.stats.last20[:1] == [True] and c.rank_pct is not None and c.rank_pct >= 0.8, difficulty="rare"),
    BadgeDef("streak_breaker", "Streak Breaker", "✂️", "tag", "Ends another tag's active 5+ win streak", lambda c: (c.upset_sigma_multiple or 0) > 0 and c.stats.streak >= 1, difficulty="rare"),
    BadgeDef("halfway_hero", "Halfway Hero", "🏁", "tag", "Reaches 50 comparisons with a winning record", lambda c: c.stats.total >= 50 and c.stats.wins > c.stats.losses, difficulty="rare"),
    BadgeDef("grand_champion", "Grand Champion", "🏆", "tag", "Its picture wins a Tournament bracket while this tag was the seed theme", lambda c: False, difficulty="legendary"),  # awarded manually, see award_manual
    BadgeDef("precision", "Precision", "🧭", "tag", "sigma below threshold and mu in the top 10% at the same time", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.9, difficulty="legendary"),
    BadgeDef("hall_of_fame", "Hall of Fame", "🏛️", "tag", "Already holds Century Club + Rock Solid + Top Dog", lambda c: {"century_club", "rock_solid", "top_dog"} <= c.held, difficulty="legendary"),
]

PICTURE_BADGES: list[BadgeDef] = [
    BadgeDef("fan_favorite", "Fan Favorite", "💖", "picture", "First to reach top-10 mu in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.8, difficulty="rare"),
    BadgeDef("photogenic", "Photogenic", "📸", "picture", "10-win streak", lambda c: c.stats.streak >= 10, difficulty="rare"),
    BadgeDef("veteran", "Veteran", "🎖️", "picture", "Survives 100 comparisons", lambda c: c.stats.total >= 100, difficulty="epic"),
    BadgeDef("dark_horse", "Dark Horse", "🐎", "picture", "Beats a much higher-rated picture", lambda c: (c.upset_sigma_multiple or 0) >= UPSET_SIGMA_MULTIPLE, difficulty="epic"),
    BadgeDef("consistent", "Consistent", "⚖️", "picture", "sigma below confidence threshold with mu in top quartile", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.75, difficulty="rare"),
    BadgeDef("tournament_champion", "Tournament Champion", "🏅", "picture", "Wins a full Tournament bracket", lambda c: c.stats.tournament_wins >= 1, difficulty="epic"),
    BadgeDef("rediscovered_gem", "Rediscovered Gem", "💎", "picture", "Resurfaced via Random Rediscovery and wins", lambda c: c.stats.rediscovery_wins >= 1, difficulty="rare"),
    BadgeDef("perfect_streak", "Perfect Streak", "🌟", "picture", "10 straight wins, no losses ever", lambda c: c.stats.streak >= 10 and c.stats.losses == 0, difficulty="epic"),
    BadgeDef("crowd_pleaser", "Crowd Pleaser", "🙌", "picture", "High mu reached with low total comparisons", lambda c: c.mu >= 30.0 and c.stats.total <= 15, difficulty="rare"),
    BadgeDef("old_reliable", "Old Reliable", "🕰️", "picture", "First rated, still top-10 after N comparisons", lambda c: c.stats.total >= 50 and c.rank_pct is not None and c.rank_pct >= 0.8, difficulty="epic"),
    BadgeDef("marathon_runner", "Marathon Runner", "🥾", "picture", "200 total comparisons", lambda c: c.stats.total >= 200, difficulty="legendary"),
    BadgeDef("untouchable_pic", "Untouchable", "❄️", "picture", "15-win streak", lambda c: c.stats.streak >= 15, difficulty="epic"),
    BadgeDef("legendary_pic", "Legendary", "🦄", "picture", "20-win streak", lambda c: c.stats.streak >= 20, difficulty="legendary"),
    BadgeDef("top_of_the_pile", "Top of the Pile", "🗻", "picture", "Reaches #1 mu in its pool", lambda c: c.rank_pct is not None and c.rank_pct >= 0.999, difficulty="legendary"),
    BadgeDef("elite_eight_pic", "Elite Eight", "🎱", "picture", "Top 8 by mu", lambda c: c.rank_pct is not None and c.rank_pct >= 0.9, difficulty="epic"),
    BadgeDef("sharpshooter_pic", "Sharpshooter", "🏹", "picture", "80%+ win rate over its last 20 comparisons", lambda c: len(c.stats.last20) >= 20 and sum(c.stats.last20) / len(c.stats.last20) >= 0.8, difficulty="rare"),
    BadgeDef("slow_burn_pic", "Slow Burn", "🐢", "picture", "Reaches confidence threshold only after 150+ comparisons", lambda c: _confident(c) and c.stats.total >= 150, difficulty="epic"),
    BadgeDef("fast_riser_pic", "Fast Riser", "🐇", "picture", "Reaches confidence threshold in under 30 comparisons", lambda c: _confident(c) and c.stats.total < 30, difficulty="rare"),
    BadgeDef("redemption_arc_pic", "Redemption Arc", "🌄", "picture", "5-loss streak recovered into a 5-win streak", lambda c: c.stats.prev_streak <= -5 and c.stats.streak >= 5, difficulty="epic"),
    BadgeDef("battle_scarred_pic", "Battle Scarred", "🧯", "picture", "Survives 20 losses, still net-positive record", lambda c: c.stats.losses >= 20 and c.stats.wins > c.stats.losses, difficulty="epic"),
    BadgeDef("bracket_buster", "Bracket Buster", "💥", "picture", "Wins a tournament match as a low/random seed against a much higher-rated pic", lambda c: False, difficulty="legendary"),  # awarded manually
    BadgeDef("finalist", "Finalist", "🥈", "picture", "Reaches a tournament final without winning it", lambda c: c.stats.tournament_finals >= 1 and c.stats.tournament_wins == 0, difficulty="rare"),
    BadgeDef("double_champion", "Double Champion", "🥇", "picture", "Wins two separate Tournament brackets", lambda c: c.stats.tournament_wins >= 2, difficulty="legendary"),
    BadgeDef("ironclad_pic", "Ironclad", "🔒", "picture", "Never drops below a positive record after its first 20 comparisons", lambda c: c.stats.total >= 20 and c.stats.wins > c.stats.losses, difficulty="rare"),
    BadgeDef("late_bloomer", "Late Bloomer", "🌸", "picture", "Starts with a loss but finishes in the top 10", lambda c: c.stats.total >= 1 and c.rank_pct is not None and c.rank_pct >= 0.8, difficulty="rare"),
    BadgeDef("streak_breaker_pic", "Streak Breaker", "🔨", "picture", "Ends another picture's active 5+ win streak", lambda c: (c.upset_sigma_multiple or 0) > 0 and c.stats.streak >= 1, difficulty="rare"),
    BadgeDef("halfway_hero_pic", "Halfway Hero", "🚩", "picture", "Reaches 50 comparisons with a winning record", lambda c: c.stats.total >= 50 and c.stats.wins > c.stats.losses, difficulty="rare"),
    BadgeDef("rediscovered_twice", "Rediscovered Twice", "🔍", "picture", "Resurfaced by Random Rediscovery more than once and wins each time", lambda c: c.stats.rediscovery_wins >= 2, difficulty="epic"),
    BadgeDef("precision_pic", "Precision", "🧿", "picture", "sigma below threshold and mu in the top 10% at the same time", lambda c: _confident(c) and c.rank_pct is not None and c.rank_pct >= 0.9, difficulty="legendary"),
    BadgeDef("hall_of_fame_pic", "Hall of Fame", "🏟️", "picture", "Already holds Veteran + Consistent + Top of the Pile", lambda c: {"veteran", "consistent", "top_of_the_pile"} <= c.held, difficulty="legendary"),
]

DIFFICULTY_ORDER: dict[Difficulty, int] = {"common": 0, "rare": 1, "epic": 2, "legendary": 3}
DIFFICULTY_COLORS: dict[Difficulty, dict[str, str]] = {
    # (background, border, text/glow) - used by the GUI for the badge pill styling.
    "common": {"bg": "rgba(90, 90, 100, 210)", "border": "#b8b8c2", "text": "#f0f0f5"},
    "rare": {"bg": "rgba(30, 70, 150, 210)", "border": "#5fa8ff", "text": "#eaf3ff"},
    "epic": {"bg": "rgba(90, 30, 150, 210)", "border": "#c77dff", "text": "#f5eaff"},
    "legendary": {"bg": "rgba(150, 95, 10, 220)", "border": "#ffc94d", "text": "#fff6e0"},
}


def rarest_badge_id(entity_type: EntityType, entity_id: str, held: set[str] | None = None) -> str | None:
    """The lowest-global-earn-count badge id an entity currently holds, or None if it holds
    none. Rarity is computed across the whole `data/badges.json` bucket for this entity_type:
    the badge_id with the fewest distinct entities holding it wins; ties break toward the
    higher difficulty tier, then alphabetically by badge_id for determinism."""
    if held is None:
        held = held_badge_ids(entity_type, entity_id)
    if not held:
        return None
    all_badges = load_badges()
    bucket = all_badges.get(entity_type + "s", {})
    counts: dict[str, int] = {}
    for entries in bucket.values():
        for entry in entries:
            bid = entry.get("badge_id")
            if bid:
                counts[bid] = counts.get(bid, 0) + 1

    def sort_key(bid: str) -> tuple[int, int, str]:
        count = counts.get(bid, 0)
        badge = BADGE_BY_ID.get(bid)
        tier = -DIFFICULTY_ORDER.get(badge.difficulty, 0) if badge is not None else 0
        return (count, tier, bid)

    return min(held, key=sort_key)

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
    pool_size: int = 0,
    upset_sigma_multiple: float | None = None,
    beat_top3: bool = False,
    is_rediscovery: bool = False,
) -> list[BadgeDef]:
    """Update one entity's running stats and evaluate its (and only its) badge conditions.

    Returns the list of newly-earned badges (empty if none). Safe to call once per entity
    per comparison - never pass both winner and loser data into a single call.
    """
    ts = ts or time.time()
    if pool_size < MIN_POOL_FOR_RANK_BADGES:
        rank_pct = None
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
