"""RatingSystem and TrueSkill scoring helpers. Pure logic: no Qt or live Hydrus client calls."""

import json
import math
import random
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import hydrus_api  # type: ignore
from trueskill import Rating, rate  # type: ignore

from config import DATA_DIR, is_filtered_tag
from tagrank.settings import Settings, load_settings

FileMetaData = dict[str, Any]

MMR_SCALE = 100


def tags_from_file(file: FileMetaData) -> list[str]:
    tag_repos: dict[str, dict[str, Any]] = file["tags"]
    tags: set[str] = set()
    for repo in tag_repos.values():
        if repo["display_tags"] is not None:
            if str(hydrus_api.TagStatus.CURRENT.value) in repo["display_tags"]:
                tags.update(tag for tag in repo["display_tags"][str(hydrus_api.TagStatus.CURRENT)]
                            if not tag.startswith("filename:") and not is_filtered_tag(tag))
            if str(hydrus_api.TagStatus.PENDING.value) in repo["display_tags"]:
                tags.update(tag for tag in repo["display_tags"][str(hydrus_api.TagStatus.PENDING)]
                            if not tag.startswith("filename:") and not is_filtered_tag(tag))
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


class RatingSystem:
    def __init__(self, client: hydrus_api.Client, file_ids: list[int], settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.client = client
        self.file_ids = file_ids
        self.used_file_pairs: set[tuple[int, int]] = set()
        self.current_ratings: dict[str, Rating] = {}
        self.file_ratings: dict[int, Rating] = {}

        ratings_path = DATA_DIR / "ratings.json"
        if ratings_path.exists():
            with open(ratings_path) as f:
                tag_to_ratings = json.loads(f.read())
                for tag, rating_params in tag_to_ratings:
                    if not tag.startswith("filename:") and not is_filtered_tag(tag):
                        self.current_ratings[tag] = Rating(rating_params[0], rating_params[1])

        self.go_back_ratings_stack: list[dict[str, Rating]] = []
        self.go_back_file_ratings_stack: list[dict[int, Rating]] = []
        self.known_comparison_choices: list[tuple[int, int]] = []

        comparisons_path = DATA_DIR / "comparisons.json"
        if comparisons_path.exists():
            try:
                with open(comparisons_path) as f:
                    comparisons = json.loads(f.read())
                    for winner, loser in comparisons:
                        self.known_comparison_choices.append((winner, loser))
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
            f.write(json.dumps([[first, second] for first, second in self.known_comparison_choices]))

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

    def get_file_pair(self) -> None | tuple[FileMetaData, FileMetaData]:
        if len(self.file_ids) < 2:
            print("Not enough files are available to create a comparison pair.")
            return None
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
        if winner_only_tags and loser_only_tags:
            winner_ratings = tuple(self.rating_for_tag(tag) for tag in winner_only_tags)
            loser_ratings = tuple(self.rating_for_tag(tag) for tag in loser_only_tags)
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
        new_winner_file_rating, new_loser_file_rating = rate(
            [[go_back_file_ratings[winner_id]], [go_back_file_ratings[loser_id]]], ranks=[0, 1]
        )
        self.file_ratings[winner_id] = new_winner_file_rating[0]
        self.file_ratings[loser_id] = new_loser_file_rating[0]

        self.go_back_ratings_stack.append(go_back_ratings)
        self.go_back_file_ratings_stack.append(go_back_file_ratings)
        self.known_comparison_choices.append((winner["file_id"], loser["file_id"]))
        self.write_file_mmr_rating(winner)
        self.write_file_mmr_rating(loser)
        self.write_file_mmr_confidence_rating(winner)
        self.write_file_mmr_confidence_rating(loser)

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
