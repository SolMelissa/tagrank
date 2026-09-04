"""RatingSystem and TrueSkill scoring helpers. Pure logic: no Qt or live Hydrus client calls."""

import json
import math
import random
import time
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import hydrus_api  # type: ignore
from trueskill import Rating, rate  # type: ignore

from config import DATA_DIR, is_excluded_tag
from tagrank import badges
from tagrank.settings import Settings, load_settings
from tagrank.tag_utils import resolve_tags

FileMetaData = dict[str, Any]

MMR_SCALE = 100


def tags_from_file(file: FileMetaData) -> list[str]:
    tag_repos: dict[str, dict[str, Any]] = file["tags"]
    tags: set[str] = set()
    for repo in tag_repos.values():
        # Use resolve_tags to get final tag set (respects siblings + namespaced carve-out)
        resolved = resolve_tags(repo)
        # Filter out filename: tags and excluded tags (TAG_FILTERS + hidden-tags marker)
        tags.update(tag for tag in resolved if not tag.startswith("filename:") and not is_excluded_tag(tag))
    return list(tags)


def trueskill_number_from_rating(rating: Rating) -> float:
    return (rating.mu - (3 * rating.sigma)) * MMR_SCALE


def trueskill_confidence_from_rating(rating: Rating) -> float:
    """Confidence in the MMR rating: higher when sigma (uncertainty) is lower.

    This is the secondary TrueSkill stat (sigma), not the rating itself (mu - 3*sigma).
    Scaled the same way as the MMR score so it is comparable/writable to Hydrus.
    """
    default_sigma = Rating().sigma
    normalized_certainty = max(0.0, min(1.0, (default_sigma - rating.sigma) / default_sigma))
    return normalized_certainty * MMR_SCALE


def trueskill_score_from_scaled_mmr(score: float) -> float:
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric_score):
        return 0.0
    return numeric_score / MMR_SCALE


def rating_from_trueskill_score(score: float) -> Rating:
    """Hydrus stores the conservative TrueSkill score as one numeric value."""
    default_sigma = Rating().sigma
    return Rating(score + (3 * default_sigma), default_sigma)


def load_current_tag_ratings() -> dict[str, Rating]:
    """Every persisted tag rating (data/ratings.json), keyed by tag - the same global store
    every RatingSystem loads at construction. Standalone so a caller that only wants tag
    scores (e.g. the rating-details API route) doesn't need a Hydrus client or file pool to
    build a whole RatingSystem just to read this file."""
    ratings: dict[str, Rating] = {}
    ratings_path = DATA_DIR / "ratings.json"
    if ratings_path.exists():
        with open(ratings_path) as f:
            tag_to_ratings = json.loads(f.read())
            for tag, rating_params in tag_to_ratings:
                if not tag.startswith("filename:") and not is_filtered_tag(tag):
                    ratings[tag] = Rating(rating_params[0], rating_params[1])
    return ratings


class RatingSystem:
    def __init__(self, client: hydrus_api.Client, file_ids: list[int], settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.client = client
        self.file_ids = file_ids
        self.used_file_pairs: set[tuple[int, int]] = set()
        self.current_ratings: dict[str, Rating] = load_current_tag_ratings()
        self.file_ratings: dict[int, Rating] = {}

        self.go_back_ratings_stack: list[dict[str, Rating]] = []
        self.go_back_file_ratings_stack: list[dict[int, Rating]] = []
        # (winner_id, loser_id, timestamp). Older comparisons.json files only have the first
        # two fields (see write_results_to_file's rename note); missing timestamps read as 0.0.
        self.known_comparison_choices: list[tuple[int, int, float]] = []
        # file_ids fed into the next get_file_pair()/process_result() specifically because
        # Random Rediscovery resurfaced them - lets badge tracking credit "rediscovery" wins
        # without Random Rediscovery needing to know anything about badges itself.
        self.pending_rediscovery_ids: set[int] = set()
        self.last_result_info: dict[str, Any] = {}
        # tag -> mu the first time it was touched this session, for the Rising Star Feed's
        # "biggest mover" delta. Session-scoped by design: it answers "what's moving right
        # now", not a full multi-session history.
        self._tag_mu_baseline: dict[str, float] = {}

        comparisons_path = DATA_DIR / "comparisons.json"
        if comparisons_path.exists():
            try:
                with open(comparisons_path) as f:
                    comparisons = json.loads(f.read())
                    for entry in comparisons:
                        winner, loser = entry[0], entry[1]
                        ts = float(entry[2]) if len(entry) > 2 else 0.0
                        self.known_comparison_choices.append((winner, loser, ts))
            except (JSONDecodeError, ValueError) as e:
                from tagrank.cli_errors import print_could_not_read_comparisons_file_help
                print_could_not_read_comparisons_file_help()
                raise e

    def process_undo(self):
        try:
            last_ratings = self.go_back_ratings_stack.pop()
            last_file_ratings = self.go_back_file_ratings_stack.pop()
            self.known_comparison_choices.pop()
        except IndexError:
            return
        for (tag, rating) in last_ratings.items():
            self.current_ratings[tag] = rating
        for file_id, rating in last_file_ratings.items():
            self.file_ratings[file_id] = rating

    def write_results_to_file(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "ratings.json", "w") as f:
            f.write(json.dumps([(tag, [rating.mu, rating.sigma]) for tag, rating in self.current_ratings.items()]))
        with open(DATA_DIR / "comparisons.json", "w") as f:
            f.write(json.dumps([[first, second, ts] for first, second, ts in self.known_comparison_choices]))

    @staticmethod
    def _bounded_ratio(value: float, scale: float) -> float:
        if not math.isfinite(value) or not math.isfinite(scale) or scale <= 0:
            return 0.0
        return max(0.0, min(1.0, abs(value) / scale))

    def build_prediction_entry(self, left_file_metadata: FileMetaData, right_file_metadata: FileMetaData, user_selection: str) -> dict[str, Any]:
        left_tag_score = average_tag_confidence(left_file_metadata, self)
        right_tag_score = average_tag_confidence(right_file_metadata, self)
        left_photo_score = self.file_score(left_file_metadata)
        right_photo_score = self.file_score(right_file_metadata)

        left_tags = tags_from_file(left_file_metadata)
        right_tags = tags_from_file(right_file_metadata)
        left_tag_certainty = sum(tag_confidence(tag, self) for tag in left_tags) / len(left_tags) if left_tags else 0.0
        right_tag_certainty = sum(tag_confidence(tag, self) for tag in right_tags) / len(right_tags) if right_tags else 0.0
        left_photo_certainty = self.file_confidence(left_file_metadata)
        right_photo_certainty = self.file_confidence(right_file_metadata)

        tag_gap = abs(left_tag_score - right_tag_score)
        photo_gap = abs(left_photo_score - right_photo_score)

        tag_prediction = "A" if left_tag_score >= right_tag_score else "B"
        photo_prediction = "A" if left_photo_score >= right_photo_score else "B"

        tag_gap_scale = max(1.0, abs(left_tag_score) + abs(right_tag_score))
        photo_gap_scale = max(1.0, abs(left_photo_score) + abs(right_photo_score))
        tag_certainty_scale = max(1.0, abs(left_tag_certainty) + abs(right_tag_certainty))
        photo_certainty_scale = max(1.0, abs(left_photo_certainty) + abs(right_photo_certainty))

        tag_gap_norm = self._bounded_ratio(tag_gap, tag_gap_scale)
        photo_gap_norm = self._bounded_ratio(photo_gap, photo_gap_scale)
        tag_certainty_norm = self._bounded_ratio((left_tag_certainty + right_tag_certainty) / 2.0, tag_certainty_scale)
        photo_certainty_norm = self._bounded_ratio((left_photo_certainty + right_photo_certainty) / 2.0, photo_certainty_scale)

        tag_confidence_component = tag_gap_norm * tag_certainty_norm
        photo_confidence_component = photo_gap_norm * photo_certainty_norm
        confidence = 0.5 * tag_confidence_component + 0.5 * photo_confidence_component

        now = datetime.now()
        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "user_selection": user_selection,
            "tag_prediction": tag_prediction,
            "photo_prediction": photo_prediction,
            "confidence": round(max(0.0, min(1.0, float(confidence))), 6),
            "tag_a_score": left_tag_score,
            "tag_b_score": right_tag_score,
            "photo_a_score": left_photo_score,
            "photo_b_score": right_photo_score,
            "tag_gap": tag_gap,
            "photo_gap": photo_gap,
            "tag_certainty_a": left_tag_certainty,
            "tag_certainty_b": right_tag_certainty,
            "photo_certainty_a": left_photo_certainty,
            "photo_certainty_b": right_photo_certainty,
            "tag_confidence_component": round(max(0.0, min(1.0, float(tag_confidence_component))), 6),
            "photo_confidence_component": round(max(0.0, min(1.0, float(photo_confidence_component))), 6),
        }

    def write_prediction_log_entry(self, left_file_metadata: FileMetaData, right_file_metadata: FileMetaData, user_selection: str):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = self.build_prediction_entry(left_file_metadata, right_file_metadata, user_selection)
        with open(DATA_DIR / "prediction_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _strategy_pair(self, strategy: str) -> tuple[int, int] | None:
        """Confidence Duel / Divergence pairing: sample a subset of unrated-pair candidates
        (full O(n^2) scoring over a 500-file pool is unnecessary work for one pairing) and
        score every candidate pair by the strategy's heuristic. Ratings for files not yet
        compared this session default to TrueSkill's Rating() prior."""
        import itertools

        sample_size = min(50, len(self.file_ids))
        candidates = random.sample(self.file_ids, k=sample_size)
        best: tuple[int, int] | None = None
        best_score: float | None = None
        for a, b in itertools.combinations(candidates, 2):
            if tuple(sorted((a, b))) in self.used_file_pairs:
                continue
            rating_a = self.file_ratings.get(a) or Rating()
            rating_b = self.file_ratings.get(b) or Rating()
            if strategy == "confidence_duel":
                # Most sigma overlap = least certain who'd win = fastest convergence.
                score = (rating_a.sigma + rating_b.sigma) - abs(rating_a.mu - rating_b.mu)
            else:  # divergence: force close-mu (possibly already-confident) items to compete
                score = -abs(rating_a.mu - rating_b.mu)
            if best_score is None or score > best_score:
                best_score, best = score, (a, b)
        return best

    def get_file_pair(self) -> None | tuple[FileMetaData, FileMetaData]:
        if len(self.file_ids) < 2:
            print("Not enough files are available to create a comparison pair.")
            return None

        strategy = self.settings.pool.pool_strategy
        if strategy in ("confidence_duel", "divergence"):
            strategy_pair = self._strategy_pair(strategy)
            if strategy_pair is not None:
                self.used_file_pairs.add(tuple(sorted(strategy_pair)))
                return self.convert_image_ids_to_file_meta_data(strategy_pair)
            # Fall through to random selection if the strategy couldn't find an unused pair
            # (e.g. every sampled combination was already compared).

        ids: list[int] = random.sample(self.file_ids, k=2)
        tries = 0
        while tuple(sorted(ids)) in self.used_file_pairs:
            if tries > 20:
                print("Tried to find a new random file pair 20 times, did not succeed.")
                return None
            ids = random.sample(self.file_ids, k=2)
            tries += 1
        self.used_file_pairs.add(tuple(sorted(ids)))
        return self.convert_image_ids_to_file_meta_data(tuple(ids))  # type: ignore

    def inject_rediscovery_pick(self, file_id: int) -> None:
        """Mark `file_id` as coming from Random Rediscovery, so a win in the next
        process_result() call credits the rediscovery-specific badges (see badges.py)."""
        self.pending_rediscovery_ids.add(file_id)

    def pick_rediscovery_file_id(self) -> int | None:
        """High-mu + oldest-last-compared file from this session's pool, per Random
        Rediscovery's design: 'surface a highly-rated but rarely-recently-seen item.'
        Falls back to just the highest-mu file if no comparison history is available yet."""
        if not self.file_ratings:
            return None
        last_seen: dict[int, float] = {}
        for winner_id, loser_id, ts in self.known_comparison_choices:
            last_seen[winner_id] = max(last_seen.get(winner_id, 0.0), ts)
            last_seen[loser_id] = max(last_seen.get(loser_id, 0.0), ts)

        ranked = sorted(
            self.file_ratings.items(), key=lambda kv: trueskill_number_from_rating(kv[1]), reverse=True
        )
        top_half = ranked[: max(1, len(ranked) // 2)]
        return min(top_half, key=lambda kv: last_seen.get(kv[0], 0.0))[0]

    def convert_image_ids_to_file_meta_data(self, pairs: tuple[int, int]) -> None | tuple[FileMetaData, FileMetaData]:
        info = self.client.get_file_metadata(file_ids=pairs)
        if info is None:
            print(f"ERROR: Was not able to find the file metadata objects for ids '{pairs}'.")
            return None
        metadata = info["metadata"]
        if metadata is None:
            print(f"ERROR: The metadata object for the file pair '{pairs}' is None!")
            return None
        if not isinstance(metadata, list):
            print(f"ERROR: The metadata object for the file pair '{pairs}' is not a list!")
            print(f"  This is what I did get: {metadata}")
            return None
        if len(metadata) != 2:
            print(f"ERROR: Did not get two metadata objects for the file pairs '{pairs}'.")
            print(f"  This is what I did get: {metadata}")
            return None
        metadata_by_id = {int(file_data.get("file_id", -1)): file_data for file_data in metadata}
        if any(file_id not in metadata_by_id for file_id in pairs):
            print(f"ERROR: Hydrus returned metadata for the wrong file ids: {metadata_by_id.keys()}.")
            return None
        return metadata_by_id[pairs[0]], metadata_by_id[pairs[1]]

    def path_from_metadata(self, file_1_metadata: FileMetaData) -> Path:
        file_id = file_1_metadata["file_id"]
        return self.client.get_file_path(file_id)["path"]

    def write_file_mmr_rating(self, file_metadata: FileMetaData) -> bool:
        file_hash = file_metadata.get("hash")
        service_key = self.settings.hydrus.mmr_service_key
        if not file_hash or not service_key or service_key == "FILL_ME_IN":
            return False

        score = self.file_score(file_metadata)
        if not math.isfinite(score):
            return False

        try:
            int_score = int(round(score))
            self.client.set_rating(service_key, int_score, hashes=[file_hash])
            print(f"TagRank MMR written for file hash '{file_hash}': {int_score}")
            return True
        except Exception as e:
            print(f"ERROR: Could not write TagRank MMR rating for hash '{file_hash}': {e}")
            return False

    def write_file_mmr_confidence_rating(self, file_metadata: FileMetaData) -> bool:
        file_hash = file_metadata.get("hash")
        service_key = self.settings.hydrus.mmr_confidence_service_key
        if not file_hash or not service_key or service_key == "FILL_ME_IN":
            return False

        confidence = self.file_confidence(file_metadata)
        if not math.isfinite(confidence):
            return False

        try:
            int_confidence = int(round(confidence))
            self.client.set_rating(service_key, int_confidence, hashes=[file_hash])
            print(f"TagRank photo MMR confidence written for file hash '{file_hash}': {int_confidence}")
            return True
        except Exception as e:
            print(f"ERROR: Could not write TagRank photo MMR confidence for hash '{file_hash}': {e}")
            return False

    def _tag_rank_pct(self, tag: str) -> float | None:
        """1.0 = best tag in the currently-known pool, 0.0 = worst. None if it's the only one."""
        if len(self.current_ratings) < 2 or tag not in self.current_ratings:
            return None
        scores = sorted(trueskill_number_from_rating(r) for r in self.current_ratings.values())
        my_score = trueskill_number_from_rating(self.current_ratings[tag])
        below = sum(1 for s in scores if s <= my_score)
        return (below - 1) / (len(scores) - 1) if len(scores) > 1 else None

    def _file_rank_pct(self, file_id: int) -> float | None:
        """Same as _tag_rank_pct but over this session's pool of pictures only (see design
        note: TagRank has no single global list of every picture's rating to rank against)."""
        if len(self.file_ratings) < 2 or file_id not in self.file_ratings:
            return None
        scores = sorted(trueskill_number_from_rating(r) for r in self.file_ratings.values())
        my_score = trueskill_number_from_rating(self.file_ratings[file_id])
        below = sum(1 for s in scores if s <= my_score)
        return (below - 1) / (len(scores) - 1) if len(scores) > 1 else None

    def _record_tag_badges(self, tag: str, *, won: bool, upset_sigma_multiple: float | None, beat_top3: bool) -> list:
        rating = self.current_ratings.get(tag)
        if rating is None:
            return []
        return badges.record_result(
            "tag", tag, won=won, mu=rating.mu, sigma=rating.sigma,
            confidence_threshold=self.settings.ui.confidence_sigma_threshold,
            rank_pct=self._tag_rank_pct(tag),
            pool_size=len(self.current_ratings),
            upset_sigma_multiple=upset_sigma_multiple, beat_top3=beat_top3,
        )

    def _record_file_badges(self, file_id: int, file_hash: str, *, won: bool, upset_sigma_multiple: float | None) -> list:
        rating = self.file_ratings.get(file_id)
        if rating is None:
            return []
        earned = badges.record_result(
            "picture", file_hash, won=won, mu=rating.mu, sigma=rating.sigma,
            confidence_threshold=self.settings.ui.confidence_sigma_threshold,
            rank_pct=self._file_rank_pct(file_id),
            pool_size=len(self.file_ratings),
            upset_sigma_multiple=upset_sigma_multiple,
            is_rediscovery=file_id in self.pending_rediscovery_ids,
        )
        if earned:
            self._sync_badges_to_hydrus(file_hash, earned)
        return earned

    def _sync_badges_to_hydrus(self, file_hash: str, earned: list) -> None:
        """Write newly-earned picture badges to Hydrus as real tags (namespace "badge:"),
        if a local tag service key has been configured (TAGRANK_BADGE_TAG_SERVICE_KEY)."""
        service_key = self.settings.hydrus.badge_tag_service_key
        if not service_key or service_key == "FILL_ME_IN" or not file_hash:
            return
        tags = [f"badge:{badge.id}" for badge in earned]
        try:
            self.client.add_tags(hashes=[file_hash], service_keys_to_tags={service_key: tags})
        except Exception as e:
            print(f"ERROR: Could not write badge tag(s) {tags} for hash '{file_hash}': {e}")

    def process_result(self, *, winner: FileMetaData, loser: FileMetaData):
        winner_tags = [
            tag for tag in tags_from_file(winner)
            if not tag.startswith("filename:") and not is_filtered_tag(tag)
        ]
        loser_tags = [
            tag for tag in tags_from_file(loser)
            if not tag.startswith("filename:") and not is_filtered_tag(tag)
        ]

        go_back_ratings: dict[str, Rating] = dict()
        winner_only_tags = sorted(set(winner_tags) - set(loser_tags))
        loser_only_tags = sorted(set(loser_tags) - set(winner_tags))

        # Snapshot pre-match state for badge/upset detection before any ratings change.
        top3_tags_before = {
            tag for tag, _r in sorted(
                self.current_ratings.items(), key=lambda kv: trueskill_number_from_rating(kv[1]), reverse=True
            )[:3]
        }
        old_loser_tag_ratings = {tag: self.rating_for_tag(tag) for tag in loser_only_tags}
        old_winner_tag_ratings = {tag: self.rating_for_tag(tag) for tag in winner_only_tags}
        for tag, rating in {**old_winner_tag_ratings, **old_loser_tag_ratings}.items():
            self._tag_mu_baseline.setdefault(tag, rating.mu)

        if winner_only_tags and loser_only_tags:
            winner_ratings = tuple(old_winner_tag_ratings[tag] for tag in winner_only_tags)
            loser_ratings = tuple(old_loser_tag_ratings[tag] for tag in loser_only_tags)
            new_winner_ratings, new_loser_ratings = rate([winner_ratings, loser_ratings], ranks=[0, 1])
            for tag, new_rating in zip(loser_only_tags, new_loser_ratings):
                if tag not in go_back_ratings:
                    go_back_ratings[tag] = self.current_ratings[tag]
                self.current_ratings[tag] = new_rating
            for tag, new_rating in zip(winner_only_tags, new_winner_ratings):
                if tag not in go_back_ratings:
                    go_back_ratings[tag] = self.current_ratings[tag]
                self.current_ratings[tag] = new_rating

        winner_id = int(winner["file_id"])
        loser_id = int(loser["file_id"])
        go_back_file_ratings = {
            winner_id: self.file_rating_for_file(winner),
            loser_id: self.file_rating_for_file(loser),
        }
        old_winner_file_rating = go_back_file_ratings[winner_id]
        old_loser_file_rating = go_back_file_ratings[loser_id]
        new_winner_file_rating, new_loser_file_rating = rate(
            [[go_back_file_ratings[winner_id]], [go_back_file_ratings[loser_id]]], ranks=[0, 1]
        )
        self.file_ratings[winner_id] = new_winner_file_rating[0]
        self.file_ratings[loser_id] = new_loser_file_rating[0]

        self.go_back_ratings_stack.append(go_back_ratings)
        self.go_back_file_ratings_stack.append(go_back_file_ratings)
        ts = time.time()
        self.known_comparison_choices.append((winner["file_id"], loser["file_id"], ts))
        self.write_file_mmr_rating(winner)
        self.write_file_mmr_rating(loser)
        self.write_file_mmr_confidence_rating(winner)
        self.write_file_mmr_confidence_rating(loser)

        # ---- Badges + Underdog Alerts (entity-scoped: each call only ever reads/writes
        # the stats of the one entity it names - see tagrank/badges.py's scoping rule). ----
        file_upset_multiple = None
        if old_loser_file_rating.sigma > 0:
            gap = old_winner_file_rating.mu - old_loser_file_rating.mu
            multiple = gap / old_loser_file_rating.sigma
            if multiple >= badges.UPSET_SIGMA_MULTIPLE:
                file_upset_multiple = multiple

        winner_file_badges = self._record_file_badges(winner_id, winner["hash"], won=True, upset_sigma_multiple=file_upset_multiple)
        loser_file_badges = self._record_file_badges(loser_id, loser["hash"], won=False, upset_sigma_multiple=None)

        winner_tag_badges: dict[str, list] = {}
        tag_upset_multiple = None
        beat_top3 = bool(top3_tags_before & set(loser_only_tags))
        for tag in winner_only_tags:
            multiple = None
            if old_loser_tag_ratings:
                avg_loser_mu = sum(r.mu for r in old_loser_tag_ratings.values()) / len(old_loser_tag_ratings)
                avg_loser_sigma = sum(r.sigma for r in old_loser_tag_ratings.values()) / len(old_loser_tag_ratings)
                if avg_loser_sigma > 0:
                    gap = old_winner_tag_ratings[tag].mu - avg_loser_mu
                    m = gap / avg_loser_sigma
                    if m >= badges.UPSET_SIGMA_MULTIPLE:
                        multiple = m
            earned = self._record_tag_badges(tag, won=True, upset_sigma_multiple=multiple, beat_top3=beat_top3)
            if earned:
                winner_tag_badges[tag] = earned

        loser_tag_badges: dict[str, list] = {}
        for tag in loser_only_tags:
            earned = self._record_tag_badges(tag, won=False, upset_sigma_multiple=None, beat_top3=False)
            if earned:
                loser_tag_badges[tag] = earned

        underdog_alert = None
        if self.settings.ui.underdog_alerts_enabled and file_upset_multiple is not None:
            underdog_alert = {
                "winner_hash": winner["hash"],
                "loser_hash": loser["hash"],
                "sigma_multiple": file_upset_multiple,
            }

        self.pending_rediscovery_ids = set()
        self.last_result_info = {
            "winner_file_badges": winner_file_badges,
            "loser_file_badges": loser_file_badges,
            "winner_tag_badges": winner_tag_badges,
            "loser_tag_badges": loser_tag_badges,
            "underdog_alert": underdog_alert,
        }

    def rising_star_feed(self, top_n: int = 10) -> list[tuple[str, float]]:
        """Biggest tag mu movers so far this session, largest gain first. Recompute and call
        this after every comparison for a live-updating feed (see summary_dashboard.py)."""
        # trueskill_number_from_rating scales mu by MMR_SCALE; baseline was captured as a
        # raw mu, so it's scaled the same way here before diffing.
        deltas = [
            (tag, trueskill_number_from_rating(rating) - self._tag_mu_baseline[tag] * MMR_SCALE)
            for tag, rating in self.current_ratings.items()
            if tag in self._tag_mu_baseline
        ]
        return sorted(deltas, key=lambda kv: kv[1], reverse=True)[:top_n]

    def rating_for_tag(self, tag: str) -> Rating:
        if tag not in self.current_ratings and not tag.startswith("filename:") and not is_filtered_tag(tag):
            self.current_ratings[tag] = Rating()
        return self.current_ratings[tag]

    def file_rating_for_file(self, file_metadata: FileMetaData) -> Rating:
        file_id = int(file_metadata["file_id"])
        if file_id not in self.file_ratings:
            ratings = file_metadata.get("ratings") or {}
            raw_score = ratings.get(self.settings.hydrus.mmr_service_key)
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                score = 0.0
            if not math.isfinite(score):
                score = 0.0
            self.file_ratings[file_id] = rating_from_trueskill_score(trueskill_score_from_scaled_mmr(score))
        return self.file_ratings[file_id]

    def file_score(self, file_metadata: FileMetaData) -> float:
        return trueskill_number_from_rating(self.file_rating_for_file(file_metadata))

    def file_confidence(self, file_metadata: FileMetaData) -> float:
        return trueskill_confidence_from_rating(self.file_rating_for_file(file_metadata))


def average_tag_confidence(file_metadata: FileMetaData, rating_system: RatingSystem) -> float:
    tags = tags_from_file(file_metadata)
    if not tags:
        return 0.0
    return sum(tag_confidence(tag, rating_system) for tag in tags) / len(tags)


def tag_confidence(tag: str, rating_system: RatingSystem) -> float:
    return trueskill_number_from_rating(rating_system.rating_for_tag(tag))


def file_tag_text(file_metadata: FileMetaData, rating_system: RatingSystem) -> str:
    tags = tags_from_file(file_metadata)
    if not tags:
        return "No visible tags\n"
    lines = []
    for tag in sorted(tags, key=lambda tag: (tag_confidence(tag, rating_system), tag), reverse=True):
        lines.append(f"{tag} (confidence: {tag_confidence(tag, rating_system):.2f})")
    return "\n".join(lines)


def format_comparison_label(
    left_photo_score: float,
    right_photo_score: float,
    left_tag_score: float,
    right_tag_score: float,
) -> str:
    left_total = left_photo_score + left_tag_score
    right_total = right_photo_score + right_tag_score

    if left_photo_score > right_photo_score:
        photo_arrow = "⟵"
    elif right_photo_score > left_photo_score:
        photo_arrow = "⟶"
    else:
        photo_arrow = "⟷"

    if left_tag_score > right_tag_score:
        tag_arrow = "⟵"
    elif right_tag_score > left_tag_score:
        tag_arrow = "⟶"
    else:
        tag_arrow = "⟷"

    if left_total > right_total:
        likely_winner = "⟵"
    elif right_total > left_total:
        likely_winner = "⟶"
    else:
        likely_winner = "⟷"

    likely_winner_html = (
        f"<span style=\"font-size: 24pt; font-weight: 700;\">{likely_winner}</span>"
    )
    return (
        "<div style='line-height:1.4; display:flex; flex-direction:column; align-items:center; gap:4px;'>"
        "<div style='width:100%; border:1px solid rgba(255,255,255,0.35); border-radius:6px; padding:4px 8px; text-align:center;'>"
        f"<div>Photo MMR</div>"
        f"<div>{left_photo_score:.2f} {photo_arrow} {right_photo_score:.2f}</div>"
        "</div>"
        "<div style='width:100%; border:1px solid rgba(255,255,255,0.35); border-radius:6px; padding:4px 8px; text-align:center;'>"
        f"<div>Tag MMR</div>"
        f"<div>{left_tag_score:.2f} {tag_arrow} {right_tag_score:.2f}</div>"
        "</div>"
        "<div style='width:100%; border:1px solid rgba(255,255,255,0.35); border-radius:6px; padding:4px 8px; text-align:center;'>"
        f"<div>Likely Winner</div>"
        f"<div>{likely_winner_html}</div>"
        "</div>"
        "</div>"
    )
