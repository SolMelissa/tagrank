"""Central settings loader for TagRank.

Two flat, extension-less files live in ./config/:
  KEYS     - secrets + connection details (git-ignored). key=value lines.
  SETTINGS - every tunable, organized by section.      key=value lines.

'#' starts a comment (full-line or trailing) in both files. Every accessor
falls back to the default passed in when a value is missing, so modules run
safely even before ensure_config_files() has created the files.
"""

from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent
DATA_DIR = CONFIG_DIR.parent / "data"


def _read(name: str) -> str | None:
    p = CONFIG_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else None


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _parse_kv(text: str) -> dict[str, str]:
    d: dict[str, str] = {}
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d


_KEYS: dict[str, str] | None = None
_SETTINGS: dict[str, str] | None = None


def _get_keys() -> dict[str, str]:
    global _KEYS
    if _KEYS is None:
        raw = _read("KEYS") or ""
        _KEYS = _parse_kv(raw)
    return _KEYS


def _get_settings() -> dict[str, str]:
    global _SETTINGS
    if _SETTINGS is None:
        raw = _read("SETTINGS") or ""
        _SETTINGS = _parse_kv(raw)
    return _SETTINGS


def key(name: str, default: str = "") -> str:
    """Fetch a secret/connection setting from KEYS."""
    return _get_keys().get(name, default)


def get(name: str, default: str = "") -> str:
    """Fetch a raw setting from SETTINGS."""
    return _get_settings().get(name, default)


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_bool(name: str, default: bool) -> bool:
    return get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def get_float_or_none(name: str, default: float | None) -> float | None:
    raw = get(name, "").strip().lower()
    if raw in ("", "none", "null", "~"):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def set_and_persist(name: str, value: object) -> None:
    """Update one SETTINGS key in memory and rewrite config/SETTINGS with the new value,
    preserving every other line as-is. Used by the settings panel / PATCH /settings so
    edits survive a restart, not just the current process."""
    global _SETTINGS
    text_value = "" if value is None else str(value)
    _get_settings()[name] = text_value

    settings_p = CONFIG_DIR / "SETTINGS"
    existing = settings_p.read_text(encoding="utf-8") if settings_p.exists() else ""
    lines = existing.splitlines()
    found = False
    for i, raw in enumerate(lines):
        if _strip_comment(raw).split("=", 1)[0].strip() == name:
            comment = raw.split("#", 1)[1] if "#" in raw else None
            new_line = f"{name} = {text_value}"
            if comment is not None:
                new_line += f" #{comment}"
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(f"{name} = {text_value}")
    settings_p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_list(name: str, default: list[str]) -> list[str]:
    raw = get(name, "")
    if not raw:
        return default
    return [p.strip() for p in raw.split(",") if p.strip()]


def get_tag_filters() -> list[str]:
    """Return all tag prefixes/exact names from config/TAG_FILTERS."""
    raw = _read("TAG_FILTERS") or ""
    filters: list[str] = []
    for line in raw.splitlines():
        value = _strip_comment(line).strip()
        if value:
            filters.append(value)
    return filters


def is_filtered_tag(tag: str) -> bool:
    """True if tag matches a filtered prefix or exact name in config/TAG_FILTERS."""
    tag = str(tag).strip()
    if not tag:
        return False
    for filter_value in get_tag_filters():
        if tag == filter_value or tag.startswith(filter_value):
            return True
    return False


def ensure_config_files() -> None:
    """Create config/ plus KEYS and SETTINGS (with sane defaults) if missing.
    Never overwrites existing files, so real keys/secrets are preserved."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    keys_p = CONFIG_DIR / "KEYS"
    if not keys_p.exists():
        keys_p.write_text(KEYS_TEMPLATE, encoding="utf-8")

    settings_p = CONFIG_DIR / "SETTINGS"
    if not settings_p.exists():
        settings_p.write_text(SETTINGS_TEMPLATE, encoding="utf-8")

    tag_filters_p = CONFIG_DIR / "TAG_FILTERS"
    if not tag_filters_p.exists():
        tag_filters_p.write_text(TAG_FILTERS_TEMPLATE, encoding="utf-8")


KEYS_TEMPLATE = """\
# ====================== CONNECTION KEYS ======================
# This file is GIT-IGNORED. It holds every secret for TagRank.
#
# Client API setup:
#   services -> review services -> local -> client api -> add -> manually
#   Permissions needed: 'search and fetch files' (+ 'edit file tags' for ranking).
#   Press apply, then add the service key and access key below.
API_URL = http://127.0.0.1:45869/
API_KEY = FILL_ME_IN
RATING_SERVICE_KEY = FILL_ME_IN
TAGRANK_MMR_SERVICE_KEY = FILL_ME_IN
TAGRANK_MMR_CONFIDENCE_SERVICE_KEY = FILL_ME_IN
# =============================================================
"""


SETTINGS_TEMPLATE = """\
# ====================== SEARCH ======================
# Comma-separated hydrus predicates. Leave SEARCH_QUERY empty to just use
# the interactive tag picker.
SEARCH_QUERY = system:number of tags > 5, system:filetype = image
DEFAULT_FILE_QUERY = system:number of tags > 5, system:filetype = image, system:limit = 5000

# ====================== POOL ASSEMBLY ======================
POOL_SIZE = 100            # final comparison pool size
CANDIDATE_SEED_COUNT = 10000   # diverse candidates fetched once
SEED_COUNT_FOR_QUERY = 10      # how many seeds to rotate through when expanding the pool
API_LIMIT_FUZZ = 2             # over-fetch multiplier to survive dedup
POOL_STRATEGY = random         # top | random | bottom | confidence_duel | divergence
MAX_TOURNAMENT_SIZE = 64       # max images randomly drawn into a Tournament Mode bracket

# ====================== DISTANCE ESCALATION ======================
MAX_DISTANCE_START = 10    # escalating floor; grows by DISTANCE_STEP below
DISTANCE_STEP = 2          # escalation step per retry
MAX_DISTANCE_HARD = 64     # hard ceiling: full Hamming range of a 64-bit phash
MIN_POOL_SATISFIED =       # percentage of pool_size to consider "enough"; 100 = 100%, 50 = 50%, 0.5 = 50%

# ====================== UI / MISC ======================
TOP_TAG_OPTIONS = 20       # how many "most liked" tags to offer
AMOUNT_OF_TAGS_IN_CHARTS = 20

CONFIDENCE_SIGMA_THRESHOLD = 3.0   # sigma below this = "Rock Solid"/confident; gates badges + Underdog Alerts
RISING_STAR_FEED_ENABLED = True    # live biggest-mover leaderboard in the summary dashboard
UNDERDOG_ALERTS_ENABLED = True     # flag upsets (mu-gap >= 3x loser's sigma) during a session

DEBUG_MODE = True
# =============================================================
"""

TAG_FILTERS_TEMPLATE = """\
# Put one tag prefix or exact tag per line.
# Matching tags will be excluded from ranking and pool selection.
hydl-import-time:
title:
bluesky post id:
hydl-sub-id:
rule34 id:
"""
