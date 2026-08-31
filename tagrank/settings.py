"""Typed Settings, grouped by the same sections as config/SETTINGS and config/KEYS.

Loaded once (see tagrank.app.main) and passed down explicitly instead of each
module calling config.key()/get_int()/... for itself.
"""

from dataclasses import dataclass

from config import get_bool, get_float_or_none, get_int, get_list, key


@dataclass(frozen=True)
class HydrusKeys:
    api_url: str
    api_key: str
    rating_service_key: str
    mmr_service_key: str
    mmr_confidence_service_key: str


@dataclass(frozen=True)
class SearchSettings:
    search_query: list[str]
    default_file_query: list[str]


@dataclass(frozen=True)
class PoolSettings:
    pool_size: int
    candidate_seed_count: int
    seed_count_for_query: int
    api_limit_fuzz: int


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


@dataclass(frozen=True)
class Settings:
    hydrus: HydrusKeys
    search: SearchSettings
    pool: PoolSettings
    distance: DistanceSettings
    ui: UiSettings


def load_settings() -> Settings:
    """Read config/KEYS and config/SETTINGS once into a typed Settings object."""
    return Settings(
        hydrus=HydrusKeys(
            api_url=key("API_URL", "http://127.0.0.1:45869/").strip(),
            api_key=key("API_KEY").strip(),
            rating_service_key=key("RATING_SERVICE_KEY").strip(),
            mmr_service_key=key("TAGRANK_MMR_SERVICE_KEY", "").strip(),
            mmr_confidence_service_key=key("TAGRANK_MMR_CONFIDENCE_SERVICE_KEY", "").strip(),
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
        ),
    )
