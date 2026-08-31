"""Typed Settings, grouped by the same sections as config/SETTINGS and config/KEYS.

Loaded once (see tagrank.app.main) and passed down explicitly instead of each
module calling config.key()/get_int()/... for itself.

For live-editable settings (the settings panel, GET/PATCH /settings), see SettingsStore
below: Settings itself stays a frozen, validated snapshot; SettingsStore holds the current
one and rebuilds-and-swaps the whole tree on an edit rather than mutating fields in place.
"""

from dataclasses import dataclass, replace

from config import get_bool, get_float_or_none, get_int, get_list, key, set_and_persist


@dataclass(frozen=True)
class HydrusKeys:
    api_url: str
    api_key: str
    rating_service_key: str
    mmr_service_key: str
    mmr_confidence_service_key: str
    badge_tag_service_key: str


@dataclass(frozen=True)
class SearchSettings:
    search_query: list[str]
    default_file_query: list[str]


#: Pairing strategies selectable in the settings panel / pool-strategy request param.
POOL_STRATEGIES = ("top", "random", "bottom", "confidence_duel", "divergence")


@dataclass(frozen=True)
class PoolSettings:
    pool_size: int
    candidate_seed_count: int
    seed_count_for_query: int
    api_limit_fuzz: int
    pool_strategy: str  # one of POOL_STRATEGIES
    max_tournament_size: int


@dataclass(frozen=True)
class DistanceSettings:
    max_distance_start: int
    distance_step: int
    max_distance_hard: int
    min_pool_satisfied: float | None


@dataclass(frozen=True)
class UiSettings:
    top_tag_options: int
    bottom_tag_options: int
    random_tag_options: int
    min_tag_file_count: int
    amount_of_tags_in_charts: int
    debug_mode: bool
    confidence_sigma_threshold: float
    rising_star_feed_enabled: bool
    underdog_alerts_enabled: bool


@dataclass(frozen=True)
class Settings:
    hydrus: HydrusKeys
    search: SearchSettings
    pool: PoolSettings
    distance: DistanceSettings
    ui: UiSettings


def _get_pool_strategy() -> str:
    from config import get
    value = get("POOL_STRATEGY", "random").strip().lower()
    return value if value in POOL_STRATEGIES else "random"


def load_settings() -> Settings:
    """Read config/KEYS and config/SETTINGS once into a typed Settings object."""
    return Settings(
        hydrus=HydrusKeys(
            api_url=key("API_URL", "http://127.0.0.1:45869/").strip(),
            api_key=key("API_KEY").strip(),
            rating_service_key=key("RATING_SERVICE_KEY").strip(),
            mmr_service_key=key("TAGRANK_MMR_SERVICE_KEY", "").strip(),
            mmr_confidence_service_key=key("TAGRANK_MMR_CONFIDENCE_SERVICE_KEY", "").strip(),
            badge_tag_service_key=key("TAGRANK_BADGE_TAG_SERVICE_KEY", "").strip(),
        ),
        search=SearchSettings(
            search_query=get_list("SEARCH_QUERY", []),
            default_file_query=get_list(
                "DEFAULT_FILE_QUERY",
                ["system:number of tags > 5", "system:filetype = image", "system:limit = 5000"],
            ),
        ),
        pool=PoolSettings(
            pool_size=get_int("POOL_SIZE", 100),
            candidate_seed_count=get_int("CANDIDATE_SEED_COUNT", 10000),
            seed_count_for_query=get_int("SEED_COUNT_FOR_QUERY", 10),
            api_limit_fuzz=get_int("API_LIMIT_FUZZ", 2),
            pool_strategy=_get_pool_strategy(),
            max_tournament_size=get_int("MAX_TOURNAMENT_SIZE", 64),
        ),
        distance=DistanceSettings(
            max_distance_start=get_int("MAX_DISTANCE_START", 10),
            distance_step=get_int("DISTANCE_STEP", 2),
            max_distance_hard=get_int("MAX_DISTANCE_HARD", 64),
            min_pool_satisfied=get_float_or_none("MIN_POOL_SATISFIED", None),
        ),
        ui=UiSettings(
            top_tag_options=get_int("TOP_TAG_OPTIONS", 20),
            bottom_tag_options=get_int("BOTTOM_TAG_OPTIONS", 10),
            random_tag_options=get_int("RANDOM_TAG_OPTIONS", 10),
            min_tag_file_count=get_int("MIN_TAG_FILE_COUNT", 1),
            amount_of_tags_in_charts=get_int("AMOUNT_OF_TAGS_IN_CHARTS", 20),
            debug_mode=get_bool("DEBUG_MODE", True),
            confidence_sigma_threshold=get_float_or_none("CONFIDENCE_SIGMA_THRESHOLD", 3.0) or 3.0,
            rising_star_feed_enabled=get_bool("RISING_STAR_FEED_ENABLED", True),
            underdog_alerts_enabled=get_bool("UNDERDOG_ALERTS_ENABLED", True),
        ),
    )


# --------------------------------------------------------------------------------------
# Live-editable settings for the settings panel / GET & PATCH /settings.
#
# Settings dataclasses above stay frozen so nothing scattered across the app becomes
# mutable piecemeal. SettingsStore holds *one* current Settings instance and replaces it
# wholesale on every edit (dataclasses.replace, nested section by section), persisting the
# changed keys back to config/SETTINGS so they survive a restart. The GUI menu bar and the
# headless API both read/write through the same store, so they're two views of one model.
# --------------------------------------------------------------------------------------

# Maps a dotted "section.field" path (as used by the settings panel/API) to the
# config/SETTINGS key it should be persisted under.
_SETTINGS_KEYS: dict[str, str] = {
    "pool.pool_size": "POOL_SIZE",
    "pool.candidate_seed_count": "CANDIDATE_SEED_COUNT",
    "pool.seed_count_for_query": "SEED_COUNT_FOR_QUERY",
    "pool.api_limit_fuzz": "API_LIMIT_FUZZ",
    "pool.pool_strategy": "POOL_STRATEGY",
    "pool.max_tournament_size": "MAX_TOURNAMENT_SIZE",
    "distance.max_distance_start": "MAX_DISTANCE_START",
    "distance.distance_step": "DISTANCE_STEP",
    "distance.max_distance_hard": "MAX_DISTANCE_HARD",
    "distance.min_pool_satisfied": "MIN_POOL_SATISFIED",
    "ui.top_tag_options": "TOP_TAG_OPTIONS",
    "ui.bottom_tag_options": "BOTTOM_TAG_OPTIONS",
    "ui.random_tag_options": "RANDOM_TAG_OPTIONS",
    "ui.min_tag_file_count": "MIN_TAG_FILE_COUNT",
    "ui.amount_of_tags_in_charts": "AMOUNT_OF_TAGS_IN_CHARTS",
    "ui.debug_mode": "DEBUG_MODE",
    "ui.confidence_sigma_threshold": "CONFIDENCE_SIGMA_THRESHOLD",
    "ui.rising_star_feed_enabled": "RISING_STAR_FEED_ENABLED",
    "ui.underdog_alerts_enabled": "UNDERDOG_ALERTS_ENABLED",
}


class SettingsStore:
    """Holds the current effective Settings and applies live edits by rebuild-and-swap."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or load_settings()

    @property
    def current(self) -> Settings:
        return self._settings

    def update(self, changes: dict[str, object]) -> Settings:
        """Apply {"pool.pool_size": 200, ...}-style edits, persist them, and swap in the
        rebuilt Settings. Unknown keys are ignored (defensive - a stale panel shouldn't crash
        a running session)."""
        new_search = self._settings.search
        new_pool = self._settings.pool
        new_distance = self._settings.distance
        new_ui = self._settings.ui

        for path, value in changes.items():
            if path not in _SETTINGS_KEYS:
                continue
            section, field_name = path.split(".", 1)
            if section == "pool":
                new_pool = replace(new_pool, **{field_name: value})
            elif section == "distance":
                new_distance = replace(new_distance, **{field_name: value})
            elif section == "ui":
                new_ui = replace(new_ui, **{field_name: value})
            elif section == "search":
                new_search = replace(new_search, **{field_name: value})
            set_and_persist(_SETTINGS_KEYS[path], value)

        self._settings = replace(
            self._settings, search=new_search, pool=new_pool, distance=new_distance, ui=new_ui
        )
        return self._settings


_default_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    """Process-wide default store, used by the GUI main window and the headless API so both
    see the same live settings without each having to thread a store through every call."""
    global _default_store
    if _default_store is None:
        _default_store = SettingsStore()
    return _default_store
