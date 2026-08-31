"""Mood/Theme Sessions: named presets bundling a pool-builder config (query, pool size,
pool strategy, distance-escalation bounds), read from config/presets.json.

Parameterizes existing pool.build_pool()/service.start_session() inputs - no new pool
logic, just a saved bundle of the knobs that already exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from config import CONFIG_DIR

PRESETS_PATH = CONFIG_DIR / "presets.json"


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    query: list[str] | None
    pool_size: int | None
    pool_strategy: str | None
    max_distance_start: int | None
    max_distance_hard: int | None


def load_presets() -> list[Preset]:
    if not PRESETS_PATH.exists():
        return []
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    presets = []
    for entry in raw:
        try:
            presets.append(Preset(
                id=entry["id"],
                name=entry["name"],
                description=entry.get("description", ""),
                query=entry.get("query"),
                pool_size=entry.get("pool_size"),
                pool_strategy=entry.get("pool_strategy"),
                max_distance_start=entry.get("max_distance_start"),
                max_distance_hard=entry.get("max_distance_hard"),
            ))
        except (KeyError, TypeError):
            continue
    return presets


def get_preset(preset_id: str) -> Preset | None:
    for preset in load_presets():
        if preset.id == preset_id:
            return preset
    return None
