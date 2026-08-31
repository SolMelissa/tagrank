import contextlib
import itertools
import json
import math
import os
import random
import sys
from datetime import datetime
from importlib.metadata import version
from json import JSONDecodeError
from pathlib import Path
from typing import Tuple, Any, NoReturn

import hydrus_api  # type: ignore
from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtGui import Qt
import matplotlib.pyplot as plt  # type: ignore
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # type: ignore
import scipy.stats as stats  # type: ignore
from trueskill import Rating, rate  # type: ignore
import numpy as np
from tagrank_pool import write_choice
from config import ensure_config_files, key, get_int, get_list, is_filtered_tag

h_api_version = version('hydrus_api')

if h_api_version is None:
    pass
elif len(h_api_version.split(".")) < 3:
    pass
else:
    try:
        major: str
        minor: str
        patch: str
        major, minor, patch = h_api_version.split(".")
        if int(major) < 5:
            print("Your hydrus_api version is not up to date!")
            print(f"Tagrank is seeing version {h_api_version}, but requires at least version 5.2.0.")
            print("You can update your hydrus_api version with the command `pip install --upgrade hydrus_api`.")
            print("If you have done so, tagrank is up to date, and this error still comes up please make a report on github or on discord.")
            print("Be sure to include the output of `pip freeze` and the error message you are now reading.")
            sys.exit(1)
    except ValueError:
        pass

DEFAULT_FILE_QUERY = get_list(
    "DEFAULT_FILE_QUERY",
    ["system:number of tags > 5", "system:filetype = image", "system:limit = 5000"],
)
AMOUNT_OF_TAGS_IN_CHARTS = get_int("AMOUNT_OF_TAGS_IN_CHARTS", 20)

mmr_service_key = key("TAGRANK_MMR_SERVICE_KEY", "").strip()
if not mmr_service_key or mmr_service_key == "FILL_ME_IN":
    print("WARNING: TAGRANK_MMR_SERVICE_KEY is not configured in config/KEYS.")
    print("  TagRank will continue without writing file MMR ratings to Hydrus until that key is set.")

mmr_confidence_service_key = key("TAGRANK_MMR_CONFIDENCE_SERVICE_KEY", "").strip()
if not mmr_confidence_service_key or mmr_confidence_service_key == "FILL_ME_IN":
    print("WARNING: TAGRANK_MMR_CONFIDENCE_SERVICE_KEY is not configured in config/KEYS.")
    print("  TagRank will continue without writing file confidence ratings to Hydrus until that key is set.")

FileMetaData = dict[str, Any]

try:
    from itertools import batched
except ImportError:
    def batched(iterable, n):
        if n < 1:
            raise ValueError('n must be at least one')
        it = iter(iterable)
        while batch := list(itertools.islice(it, n)):
            yield batch


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


class RatingSystem:
    def __init__(self, client: hydrus_api.Client, file_ids: list[int]):
        self.client = client
        self.file_ids = file_ids
        self.used_file_pairs: set[tuple[int, int]] = set()
        self.current_ratings: dict[str, Rating] = {}
        self.file_ratings: dict[int, Rating] = {}

        if Path("./ratings.json").exists():
            with open(Path("./ratings.json")) as f:
                tag_to_ratings = json.loads(f.read())
                for tag, rating_params in tag_to_ratings:
                    if not tag.startswith("filename:") and not is_filtered_tag(tag):
                        self.current_ratings[tag] = Rating(rating_params[0], rating_params[1])

        self.go_back_ratings_stack: list[dict[str, Rating]] = []
        self.go_back_file_ratings_stack: list[dict[int, Rating]] = []
        self.known_comparison_choices: list[Tuple[int, int]] = []

        if Path("./comparisons.json").exists():
            try:
                with open(Path("./comparisons.json")) as f:
                    comparisons = json.loads(f.read())
                    for winner, loser in comparisons:
                        self.known_comparison_choices.append((winner, loser))
            except (JSONDecodeError, ValueError) as e:
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
        with open(Path("./ratings.json"), "w") as f:
            f.write(json.dumps([(tag, [rating.mu, rating.sigma]) for tag, rating in self.current_ratings.items()]))
        with open(Path("./comparisons.json"), "w") as f:
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
        log_path = Path("./prediction_log.json")
        try:
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8") as f:
                    existing_log = json.loads(f.read() or "[]")
            else:
                existing_log = []
            if not isinstance(existing_log, list):
                existing_log = []
        except (JSONDecodeError, ValueError):
            existing_log = []

        entry = self.build_prediction_entry(left_file_metadata, right_file_metadata, user_selection)
        existing_log.append(entry)

        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(existing_log, f, indent=2)

    def get_file_pair(self) -> None | Tuple[FileMetaData, FileMetaData]:
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

    def convert_image_ids_to_file_meta_data(self, pairs: Tuple[int, int]) -> None | Tuple[FileMetaData, FileMetaData]:
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
        service_key = key("TAGRANK_MMR_SERVICE_KEY", "").strip()
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
        service_key = key("TAGRANK_MMR_CONFIDENCE_SERVICE_KEY", "").strip()
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
            mmr_key = key("TAGRANK_MMR_SERVICE_KEY", "").strip()
            raw_score = ratings.get(mmr_key)
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


class Window(QtWidgets.QWidget):
    def __init__(self, rating_system: RatingSystem, client: hydrus_api.Client, dashboard: "SummaryDashboard | None" = None):
        super().__init__()
        self.client = client
        self.left_file_metadata: FileMetaData = {}
        self.right_file_metadata: FileMetaData = {}
        self.rating_system: RatingSystem = rating_system
        self.dashboard = dashboard
        self.go_back_image_pairs_stack: list[Tuple[int, int]] = []
        self.comparisons = 0
        self._last_scaled_pair: tuple[int, int] | None = None
        self.set_window_title_based_on_comparison_count()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.main_layout = QtWidgets.QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(8)

        self.comparison_surface = QtWidgets.QWidget()
        self.comparison_layout = QtWidgets.QStackedLayout(self.comparison_surface)
        self.comparison_layout.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        self.main_layout.addWidget(self.comparison_surface, 1)

        self.image_container = QtWidgets.QWidget()
        self.image_layout = QtWidgets.QHBoxLayout(self.image_container)
        self.image_layout.setContentsMargins(0, 0, 0, 0)
        self.image_layout.setSpacing(8)
        self.comparison_layout.addWidget(self.image_container)

        self.leftImageLabel = QtWidgets.QLabel("left image")
        self.rightImageLabel = QtWidgets.QLabel("right image")
        self.leftImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rightImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.leftImageLabel.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.rightImageLabel.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.leftImageLabel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.rightImageLabel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.leftImageLabel.setStyleSheet("QLabel { background: black; }")
        self.rightImageLabel.setStyleSheet("QLabel { background: black; }")
        self.image_layout.addWidget(self.leftImageLabel, 1)
        self.image_layout.addWidget(self.rightImageLabel, 1)
        for label in [self.leftImageLabel, self.rightImageLabel]:
            label.setMinimumWidth(260)
            label.setMinimumHeight(360)

        self.overlay_container = QtWidgets.QWidget(self)
        self.overlay_container.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.overlay_container.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.overlay_container.setStyleSheet("QWidget { background: transparent; }")
        self.overlay_layout = QtWidgets.QGridLayout(self.overlay_container)
        self.overlay_layout.setContentsMargins(12, 12, 12, 12)
        self.overlay_layout.setSpacing(6)

        self.overlay_panel = QtWidgets.QFrame(self.overlay_container)
        self.overlay_panel.setObjectName("comparisonOverlay")
        self.overlay_panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.overlay_panel.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.overlay_panel.setStyleSheet(
            "QFrame#comparisonOverlay {"
            "  background: rgba(20, 20, 20, 210);"
            "  border: 1px solid rgba(255,255,255,110);"
            "  border-radius: 8px;"
            "  color: white;"
            "}"
            "QLabel { color: white; }"
        )
        self.overlay_panel.setFrameShape(QtWidgets.QFrame.Shape.Box)
        self.overlay_panel.setMaximumWidth(900)
        self.overlay_panel_layout = QtWidgets.QHBoxLayout(self.overlay_panel)
        self.overlay_panel_layout.setContentsMargins(10, 8, 10, 8)
        self.overlay_panel_layout.setSpacing(10)

        self.left_tags_box = QtWidgets.QPlainTextEdit()
        self.right_tags_box = QtWidgets.QPlainTextEdit()
        self.comparison_label = QtWidgets.QLabel()
        self.comparison_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.comparison_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.comparison_label.setWordWrap(True)
        self.comparison_label.setMinimumWidth(150)
        self.comparison_label.setMaximumWidth(180)
        self.comparison_label.setStyleSheet("QLabel { font-size: 9pt; font-weight: bold; }")
        self.comparison_label.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        for tags_box in (self.left_tags_box, self.right_tags_box):
            tags_box.setReadOnly(True)
            tags_box.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
            tags_box.setMaximumHeight(180)
            tags_box.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            tags_box.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            tags_box.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            tags_box.setStyleSheet(
                "QPlainTextEdit { background: transparent; color: white; border: 0; font-size: 8.5pt; }"
            )

        self.overlay_panel_layout.addWidget(self.left_tags_box, 1)
        self.overlay_panel_layout.addWidget(self.comparison_label)
        self.overlay_panel_layout.addWidget(self.right_tags_box, 1)
        self.overlay_layout.addWidget(self.overlay_panel, 0, 0, 1, 2, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.overlay_layout.setRowStretch(0, 0)
        self.overlay_layout.setRowStretch(1, 1)
        self.overlay_container.raise_()
        self.comparison_layout.addWidget(self.overlay_container)
        self.comparison_layout.setCurrentWidget(self.overlay_container)

        self.store_metadata_and_show_images_for_comparison_pair(self.rating_system.get_file_pair())

    def set_window_title_based_on_comparison_count(self):
        self.setWindowTitle(f"TagRank - Comparisons done this session: {self.comparisons}")

    def refresh_comparison_details(self):
        left_tags = file_tag_text(self.left_file_metadata, self.rating_system)
        right_tags = file_tag_text(self.right_file_metadata, self.rating_system)
        self.left_tags_box.setPlainText(left_tags)
        self.right_tags_box.setPlainText(right_tags)

        left_photo_score = self.rating_system.file_score(self.left_file_metadata)
        right_photo_score = self.rating_system.file_score(self.right_file_metadata)
        left_tag_score = average_tag_confidence(self.left_file_metadata, self.rating_system)
        right_tag_score = average_tag_confidence(self.right_file_metadata, self.rating_system)
        self.comparison_label.setText(
            format_comparison_label(
                left_photo_score,
                right_photo_score,
                left_tag_score,
                right_tag_score,
            )
        )
        self.overlay_panel.setVisible(True)

    def store_image_pair_onto_undo_stack(self, left_metadata: FileMetaData, right_metadata: FileMetaData):
        left_id = left_metadata["file_id"]
        right_id = right_metadata["file_id"]
        self.go_back_image_pairs_stack.append((left_id, right_id))

    def store_metadata_and_show_images_for_comparison_pair(self, metadatas: Tuple[FileMetaData, FileMetaData] | None):
        if metadatas is None:
            print("Was, for any reason, not able to load a pair of files. Shutting down now.")
            self.exit()
            return
        self.left_file_metadata, self.right_file_metadata = metadatas
        self.apply_image_pair_pixmaps()
        self.refresh_comparison_details()
        self.setFocus()

    def apply_image_pair_pixmaps(self):
        if not self.left_file_metadata or not self.right_file_metadata:
            return
        left_file_path = self.rating_system.path_from_metadata(self.left_file_metadata)
        right_file_path = self.rating_system.path_from_metadata(self.right_file_metadata)
        left_pair = (int(self.left_file_metadata["file_id"]), int(self.right_file_metadata["file_id"]))
        if self._last_scaled_pair == left_pair and self.leftImageLabel.pixmap() is not None and self.rightImageLabel.pixmap() is not None:
            return
        self.leftImageLabel.setPixmap(
            QtGui.QPixmap(left_file_path).scaled(self.leftImageLabel.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.FastTransformation))
        self.rightImageLabel.setPixmap(
            QtGui.QPixmap(right_file_path).scaled(self.rightImageLabel.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.FastTransformation))
        self._last_scaled_pair = left_pair

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        if self.left_file_metadata and self.right_file_metadata:
            self.apply_image_pair_pixmaps()

    def process_undo(self):
        try:
            image_ids = self.go_back_image_pairs_stack.pop()
        except IndexError:
            return
        meta_datas = self.rating_system.convert_image_ids_to_file_meta_data(image_ids)
        self.rating_system.process_undo()
        if meta_datas is not None:
            for file_metadata in meta_datas:
                self.rating_system.write_file_mmr_rating(file_metadata)
        self.store_metadata_and_show_images_for_comparison_pair(meta_datas)
        self.comparisons -= 1
        self.set_window_title_based_on_comparison_count()
        if self.dashboard is not None:
            self.dashboard.refresh()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        if key == QtCore.Qt.Key.Key_Left or key == QtCore.Qt.Key.Key_A:
            self.rating_system.write_prediction_log_entry(self.left_file_metadata, self.right_file_metadata, "A")
            self.rating_system.process_result(winner=self.left_file_metadata, loser=self.right_file_metadata)
            write_choice(self.left_file_metadata["hash"], liked=True, client=self.client)
            write_choice(self.right_file_metadata["hash"], liked=False, client=self.client)
        elif key == QtCore.Qt.Key.Key_Right or key == QtCore.Qt.Key.Key_D:
            self.rating_system.write_prediction_log_entry(self.left_file_metadata, self.right_file_metadata, "B")
            self.rating_system.process_result(winner=self.right_file_metadata, loser=self.left_file_metadata)
            write_choice(self.right_file_metadata["hash"], liked=True, client=self.client)
            write_choice(self.left_file_metadata["hash"], liked=False, client=self.client)
        elif key == QtCore.Qt.Key.Key_Down or key == QtCore.Qt.Key.Key_S:
            event.accept()
            return
        elif key == QtCore.Qt.Key.Key_Escape:
            self.exit()
            return
        elif key == QtCore.Qt.Key.Key_Backspace or key == QtCore.Qt.Key.Key_R:
            self.process_undo()
            return
        elif key == QtCore.Qt.Key.Key_O:
            self.open_files_externally()
            return
        else:
            event.ignore()
            return
        event.accept()
        self.comparisons += 1
        self.set_window_title_based_on_comparison_count()
        self.store_image_pair_onto_undo_stack(self.left_file_metadata, self.right_file_metadata)
        self.store_metadata_and_show_images_for_comparison_pair(self.rating_system.get_file_pair())
        if self.dashboard is not None:
            self.dashboard.refresh()

    def open_files_externally(self) -> None:
        file_path_right = "file://" + str(self.rating_system.path_from_metadata(self.right_file_metadata).resolve())
        file_path_left = "file://" + str(self.rating_system.path_from_metadata(self.left_file_metadata).resolve())
        try:
            os.startfile(file_path_left)
            os.startfile(file_path_right)
        except AttributeError:
            with contextlib.redirect_stdout:
                QtGui.QDesktopServices.openUrl(file_path_left)
                QtGui.QDesktopServices.openUrl(file_path_right)

    def exit(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:
        self.prepare_to_quit()

    def prepare_to_quit(self):
        print("Saving results to file...")
        self.rating_system.write_results_to_file()


def load_prediction_entries() -> list[dict[str, Any]]:
    prediction_log_path = Path("./prediction_log.json")
    if not prediction_log_path.exists():
        return []
    try:
        with open(prediction_log_path, "r", encoding="utf-8") as f:
            raw_entries = json.loads(f.read() or "[]")
        if isinstance(raw_entries, list):
            return [entry for entry in raw_entries if isinstance(entry, dict)]
    except (JSONDecodeError, ValueError):
        pass
    return []


def top_tags_from_rating_system(rating_system: "RatingSystem", amount_of_tags: int = AMOUNT_OF_TAGS_IN_CHARTS) -> list[Tuple[str, Rating]]:
    return sorted(rating_system.current_ratings.items(),
                  key=lambda x: trueskill_number_from_rating(x[1]),
                  reverse=True)[:max(10, amount_of_tags)]


class SummaryDashboard(QtWidgets.QWidget):
    """Companion window that shows all session summary graphs together in one 2x2 grid, updated live."""

    def __init__(self, rating_system: RatingSystem, amount_of_tags: int):
        super().__init__()
        self.rating_system = rating_system
        self.amount_of_tags = amount_of_tags
        self.setWindowTitle("TagRank - Session Summary")
        self.resize(1100, 800)

        self.grid_layout = QtWidgets.QGridLayout(self)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setSpacing(4)
        self.canvases: list[FigureCanvasQTAgg] = []

        self.refresh()

    def refresh(self) -> None:
        prediction_entries = load_prediction_entries()
        top_tags = top_tags_from_rating_system(self.rating_system, self.amount_of_tags)

        figures = build_session_summary_figures(prediction_entries, top_tags, figure_height=350)

        old_canvases = self.canvases
        self.canvases = []
        for index, figure in enumerate(figures):
            canvas = FigureCanvasQTAgg(figure)
            self.grid_layout.addWidget(canvas, index // 2, index % 2)
            self.canvases.append(canvas)

        for old_canvas in old_canvases:
            self.grid_layout.removeWidget(old_canvas)
            old_figure = old_canvas.figure
            old_canvas.setParent(None)
            plt.close(old_figure)


def print_could_not_read_comparisons_file_help() -> None:
    print(f"ERROR: Was not able to read your comparisons.json file!")
    print(f"  The reason for this will be printed above, or below this information.")
    print(f"  If you do not know what the reason means you should do the following:")
    print(f"  1. Rename the file {Path('./comparisons.json').resolve()} to something else.")
    print(f"  2. Show the error and the file to me in the hydrus discord if you want to recover the comparisons.")
    print(f"  3. Re-open TagRank, it will start your comparisons list from new.", flush=True)


def print_access_key_info_then_exit() -> NoReturn:
    print("  You need to create a client api service via services->review services->local->client api->add->manually")
    print("  It needs to have the permission search and fetch files.")
    print("  You can blacklist any tags you want, but they won't get ranked if this program cannot see them.")
    print("  When you have done this, open the config/KEYS file (created on first run)")
    print("  and set API_KEY to your access key. The file is git-ignored.")
    print("  Then exit these windows by pressing apply.")
    print()
    print("  Now you need to turn on the client API.")
    print_enable_client_api_help()
    print()
    print("  If you have a non-standard URL or PORT set API_URL in config/KEYS too,")
    print("  e.g. 'http://127.0.0.1:45869/'.")
    sys.exit(0)


def print_verification_server_error_help_then_exit(e: None | hydrus_api.ServerError = None) -> NoReturn:
    print("ERROR: Something went wrong trying to verify your access key.")
    print("  Try re-creating your client api and saving the new access key. If need info on how. Re-set API_KEY in config/KEYS and restart TagRank.")
    if e is not None:
        print("  If that does not solve your issue, then look at the error that hydrus gave me below.")
        print("  Read it all, but the last line is probably where you'll find what is wrong.")
        print("This is what the server told me:")
        print(e)
    sys.exit(0)


def print_connection_error_help_then_exit(e: hydrus_api.ConnectionError) -> NoReturn:
    print("ERROR: Was not able to connect to hydrus.")
    print("  Are you sure your hydrus client is on?")
    print("  If it is, ensure that the API itself is on.")
    print_enable_client_api_help()
    print("  This is the error that caused the connection problem:")
    print(e)
    sys.exit(0)


def print_enable_client_api_help():
    print("  Go to Services -> Manage Services -> (double click) client api.")
    print("  Then ensure that the 'run the client api?' tick-box is on.")
    print("  Exit these windows by pressing apply.")


def print_permissions_error_then_exit(e: (hydrus_api.InsufficientAccess | None) = None) -> NoReturn:
    print("ERROR: This access key is not allowed to search for and fetch files.")
    print("  Please allow this permission for the access key you set in the config/KEYS file.")
    print("  You can find this setting at: services->review services->local->client api")
    print()
    if e is not None:
        print("We know this because the client returned the following error: ")
        print(e)
    sys.exit(0)


def print_no_relevant_files_then_exit(query: list[str]) -> NoReturn:
    print(f"ERROR: Was not able to find enough files in the client to compare.")
    print(f"  Are you sure I am allowed to search for files?")
    print(f"  I am specifically searching for files that are found by searching for the following query:")
    print(f"  {', '.join(query)}")
    print(f"  If this query looks weird, check your selection.")
    sys.exit(0)


def print_empty_query_help_then_exit() -> NoReturn:
    print("ERROR: the file query is empty.")
    print("Since this may lead to very large queries, this is not allowed.")
    print("If you really want the search to return all files, add 'system: everything'.")
    sys.exit(0)


def print_could_not_fetch_file_information_then_exit() -> NoReturn:
    print("ERROR: Was not able to fetch file information.")
    print("  Are you sure that I have all the needed permissions?")
    sys.exit(0)


def print_no_relevant_files_to_sort_then_exit() -> NoReturn:
    print("ERROR: Was not able to find any files to sort.")
    print("  Are you sure you have any ranked tags?")
    sys.exit(0)


def print_add_tags_permissions_missing_info_then_exit() -> NoReturn:
    print("ERROR: TagRank is not allowed to add tags to the client!")
    print("  In order to add the ranking tags to the client TagRank needs the 'edit file tags' permission.")
    print("  You can set this up by going to the following:")
    print("  Services -> Review Services -> local -> client api")
    print("  In this window, select the TagRank client api, then press 'edit' at the bottom of the screen.")
    print("  Now, in this window, check the checkbox before 'edit file tags'.")
    print("  Exit the window by pressing 'apply', then press 'close' to close the review services window.")
    print("  After you've done that, re-run TagRank.")
    sys.exit(0)


MMR_SCALE = 100


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


def average_tag_confidence(file_metadata: FileMetaData, rating_system: RatingSystem) -> float:
    tags = tags_from_file(file_metadata)
    if not tags:
        return 0.0
    return sum(tag_confidence(tag, rating_system) for tag in tags) / len(tags)


def tag_confidence(tag: str, rating_system: RatingSystem) -> float:
    return trueskill_number_from_rating(rating_system.rating_for_tag(tag))


def rating_from_trueskill_score(score: float) -> Rating:
    """Hydrus stores the conservative TrueSkill score as one numeric value."""
    default_sigma = Rating().sigma
    return Rating(score + (3 * default_sigma), default_sigma)


def file_tag_text(file_metadata: FileMetaData, rating_system: RatingSystem) -> str:
    tags = tags_from_file(file_metadata)
    if not tags:
        return "No visible tags\n"
    lines = []
    for tag in sorted(tags, key=lambda tag: (tag_confidence(tag, rating_system), tag), reverse=True):
        lines.append(f"{tag} (confidence: {tag_confidence(tag, rating_system):.2f})")
    return "\n".join(lines)


def create_client_or_exit() -> hydrus_api.Client:
    access_key = key("API_KEY").strip()
    if not access_key or access_key == "FILL_ME_IN":
        print("ERROR: No API_KEY found in the config/KEYS file.")
        print_access_key_info_then_exit()
    url = key("API_URL").strip() or None
    client = hydrus_api.Client(access_key, api_url=url) if url else hydrus_api.Client(access_key)
    access_key_response = None
    try:
        access_key_response = client.verify_access_key()
    except hydrus_api.ServerError as e:
        print_verification_server_error_help_then_exit(e)
    except hydrus_api.ConnectionError as e:
        print_connection_error_help_then_exit(e)
    except hydrus_api.InsufficientAccess as e:
        print_permissions_error_then_exit(e)
    if access_key_response is None:
        print_verification_server_error_help_then_exit()
    if 3 not in access_key_response["basic_permissions"]:
        print_permissions_error_then_exit(None)
    return client


def calculate_tag_count_for_height(available_height_px: int | None = None, min_tags: int = 5, max_tags: int = 20) -> int:
    if available_height_px is None:
        return max_tags
    try:
        available_height_px = int(available_height_px)
    except (TypeError, ValueError):
        return max_tags
    if available_height_px <= 0:
        return min_tags
    min_height = 350
    max_height = 1200
    clamped_height = min(max(available_height_px, min_height), max_height)
    normalized = (clamped_height - min_height) / (max_height - min_height)
    tag_count = min_tags + round(normalized * (max_tags - min_tags))
    return max(min_tags, min(max_tags, tag_count))


def _create_placeholder_figure(title: str, message: str) -> plt.Figure:
    """Create an informational placeholder figure for when data is unavailable."""
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.text(0.5, 0.6, message, ha='center', va='center', fontsize=14, style='italic', color='#666666')
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.axis('off')
    fig.tight_layout()
    return fig


def build_session_summary_figures(
    prediction_entries: list[dict[str, Any]],
    top_tags: list[tuple[str, Rating]],
    *,
    figure_height: int = 700,
) -> list[plt.Figure]:
    if not isinstance(prediction_entries, list):
        prediction_entries = []
    figures: list[plt.Figure] = []

    # Figure 1: Rolling prediction accuracy or placeholder
    if prediction_entries:
        tag_correct = []
        photo_correct = []
        combo_correct = []
        running_tag = 0.0
        running_photo = 0.0
        running_combined = 0.0

        for index, entry in enumerate(prediction_entries, start=1):
            tag_is_correct = bool(entry.get("tag_prediction") == entry.get("user_selection"))
            photo_is_correct = bool(entry.get("photo_prediction") == entry.get("user_selection"))
            combined = (1.0 if tag_is_correct or photo_is_correct else 0.0)
            tag_correct.append(tag_is_correct)
            photo_correct.append(photo_is_correct)
            combo_correct.append(combined)
            running_tag += (1.0 if tag_is_correct else 0.0)
            running_photo += (1.0 if photo_is_correct else 0.0)
            running_combined += combined
            tag_accuracy = running_tag / index
            photo_accuracy = running_photo / index
            overall_accuracy = running_combined / index
            tag_correct[index - 1] = tag_accuracy
            photo_correct[index - 1] = photo_accuracy
            combo_correct[index - 1] = overall_accuracy

        fig, axes = plt.subplots(figsize=(11, 5))
        x_values = list(range(1, len(prediction_entries) + 1))
        axes.plot(x_values, tag_correct, label="Tag model", color="#4C78A8", linewidth=2.0, marker='o', markersize=3, alpha=0.7)
        axes.plot(x_values, photo_correct, label="Photo model", color="#F58518", linewidth=2.0, marker='s', markersize=3, alpha=0.7)
        axes.plot(x_values, combo_correct, label="Combined prediction", color="#54A24B", linewidth=2.5, marker='^', markersize=4, alpha=0.8)
        axes.set_title("Rolling Prediction Accuracy Over Time", fontsize=12, fontweight='bold')
        axes.set_xlabel("Comparison Number", fontsize=10)
        axes.set_ylabel("Cumulative Accuracy", fontsize=10)
        axes.set_ylim(-0.05, 1.1)
        axes.grid(True, alpha=0.2, linestyle='--')
        axes.legend(loc='lower right', fontsize=9)
        fig.tight_layout()
        figures.append(fig)
    else:
        figures.append(_create_placeholder_figure(
            "Rolling Prediction Accuracy Over Time",
            "No prediction data yet.\nComplete a ranking session to see accuracy trends."
        ))

    # Figure 2: Ratings per date or placeholder
    if prediction_entries:
        date_counter: dict[str, int] = {}
        for entry in prediction_entries:
            date_value = str(entry.get("date") or "unknown")
            date_counter[date_value] = date_counter.get(date_value, 0) + 1
        dates = list(date_counter.keys())
        counts = [date_counter[date] for date in dates]

        fig, ax = plt.subplots(figsize=(11, 5))
        bars = ax.bar(dates, counts, color="#72B7B2", edgecolor="#4A8A84", linewidth=1.5)
        ax.set_title("Ratings per Session Date", fontsize=12, fontweight='bold')
        ax.set_xlabel("Date", fontsize=10)
        ax.set_ylabel("Number of Comparisons", fontsize=10)
        ax.grid(axis="y", alpha=0.2, linestyle='--')

        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{int(height)}',
                   ha='center', va='bottom', fontsize=8)

        fig.autofmt_xdate(rotation=45, ha='right')
        fig.tight_layout()
        figures.append(fig)
    else:
        figures.append(_create_placeholder_figure(
            "Ratings per Session Date",
            "No prediction data yet.\nComplete a ranking session to see activity by date."
        ))

    # Figure 3: Confidence calibration or placeholder
    calibration_figure = None
    if prediction_entries:
        bins = np.linspace(0, 1, 11)
        calibration_values = []
        calibration_labels = []
        sample_counts = []

        for start, end in zip(bins[:-1], bins[1:]):
            bucket = [entry for entry in prediction_entries if start <= float(entry.get("confidence", 0.0)) < end]
            if not bucket:
                accuracy = 0.0
                sample_counts.append(0)
            else:
                correctness = [1.0 if entry.get("tag_prediction") == entry.get("user_selection") else 0.0 for entry in bucket]
                accuracy = float(sum(correctness) / len(correctness))
                sample_counts.append(len(bucket))

            calibration_values.append(accuracy)
            calibration_labels.append(f"{start:.1f}–{end:.1f}")

        if len(calibration_values) > 0 and sum(sample_counts) > 0:
            fig, ax = plt.subplots(figsize=(11, 5))
            colors = ["#E45756" if v > 0 else "#CCCCCC" for v in calibration_values]
            bars = ax.bar(calibration_labels, calibration_values, color=colors, edgecolor="#A83339", linewidth=1.2)

            ax.axhline(0.5, color="black", linestyle="--", linewidth=1.5, alpha=0.6, label="Perfect calibration")

            ax.set_title("Tag Model Confidence Calibration", fontsize=12, fontweight='bold')
            ax.set_xlabel("Confidence Bin", fontsize=10)
            ax.set_ylabel("Observed Correctness", fontsize=10)
            ax.set_ylim(-0.05, 1.1)
            ax.grid(axis="y", alpha=0.2, linestyle='--')
            ax.legend(loc='upper left', fontsize=9)

            for bar, count in zip(bars, sample_counts):
                if count > 0:
                    height = bar.get_height()
                    ax.text(bar.get_x() + bar.get_width()/2., height + 0.02,
                           f'n={count}',
                           ha='center', va='bottom', fontsize=8, style='italic')

            fig.tight_layout()
            calibration_figure = fig
    if calibration_figure is None:
        calibration_figure = _create_placeholder_figure(
            "Tag Model Confidence Calibration",
            "No prediction data yet.\nComplete a ranking session to see calibration analysis."
        )
    figures.append(calibration_figure)

    # Figure 4: Final tag rankings by TrueSkill score
    ranked_tags = sorted(top_tags, key=lambda tag_rating: trueskill_number_from_rating(tag_rating[1]), reverse=True)
    visible_count = calculate_tag_count_for_height(figure_height)
    available_tags = ranked_tags[:visible_count]
    total_ranked_tags = len(ranked_tags)

    fig, ax = plt.subplots(figsize=(10, max(5.0, 0.35 * len(available_tags) + 1.5)))
    labels = [tag for tag, _ in available_tags]
    scores = [trueskill_number_from_rating(rating) for _, rating in available_tags]

    bars = ax.barh(labels[::-1], scores[::-1], color="#B279A2", edgecolor="#7D4F77", linewidth=1.2)
    ax.invert_yaxis()

    for i, (bar, score) in enumerate(zip(bars, scores[::-1])):
        ax.text(score + 0.3, bar.get_y() + bar.get_height()/2.,
               f'{score:.1f}',
               ha='left', va='center', fontsize=9, fontweight='bold')

    title_suffix = f"(showing {len(labels)} of {total_ranked_tags})" if len(labels) < total_ranked_tags else f"(all {total_ranked_tags})"
    ax.set_title(f"Final Tag Rankings by TrueSkill Score\n{title_suffix}", fontsize=12, fontweight='bold')
    ax.set_xlabel("TrueSkill Score", fontsize=10)
    ax.set_ylabel("Tag", fontsize=10)
    ax.grid(axis="x", alpha=0.2, linestyle='--')
    ax.set_xlim(0, max(scores) * 1.15 if scores else 10)

    fig.tight_layout()
    figures.append(fig)

    return figures


def run_for_rank_tags(client) -> None:
    files_path_path = Path("./FILES_PATH")
    if files_path_path.exists():
        print("WARNING: The `./FILES_PATH` file is no longer needed. You can remove it.")
        print(f"         The exact path is: {files_path_path.resolve()}")

    from tagrank_pool import build_pool, prompt_for_search
    query = prompt_for_search(client)  # numbered most-liked tags, 0 = custom search
    hashes = build_pool(client=client, query=query)
    if not hashes:
        print_no_relevant_files_then_exit(query)

    metadata_response = client.get_file_metadata(hashes=hashes)
    if metadata_response is None or metadata_response.get("metadata") is None:
        print_could_not_fetch_file_information_then_exit()

    ids = [int(meta["file_id"]) for meta in metadata_response["metadata"] if "file_id" in meta]

    if len(ids) < 2:
        print_no_relevant_files_then_exit(query)

    app = QtWidgets.QApplication(sys.argv)
    rating_system = RatingSystem(client, ids)

    dashboard = SummaryDashboard(rating_system, AMOUNT_OF_TAGS_IN_CHARTS)
    window: Window = Window(rating_system, client, dashboard)

    window.show()
    screen_geometry = window.screen().availableGeometry() if window.screen() else None
    if screen_geometry is not None:
        window.setGeometry(
            screen_geometry.x(), screen_geometry.y(),
            screen_geometry.width() * 3 // 5, screen_geometry.height()
        )
        dashboard.setGeometry(
            screen_geometry.x() + screen_geometry.width() * 3 // 5, screen_geometry.y(),
            screen_geometry.width() * 2 // 5, screen_geometry.height()
        )
    dashboard.show()

    first_section_result = app.exec()
    if first_section_result != 0:
        print("Comparison app closed in error. Not moving on to comparisons.")
        sys.exit(first_section_result)
    window.destroy()

    many_tags: list[Tuple[str, Rating]] = top_tags_from_rating_system(rating_system, AMOUNT_OF_TAGS_IN_CHARTS)

    largest_mu_width = len(str(math.floor(trueskill_number_from_rating(many_tags[0][1]))))
    print("The window that shows the scores can be hard to read. So here the data in text for 10 tags:")
    for (tag, rating) in many_tags:
        print(f"{trueskill_number_from_rating(rating):.1f}".rjust(largest_mu_width + 3) + f": {tag}")


def sort_files_by_mmr(
    file_infos: list[Tuple[int, FileMetaData]], rating_system: RatingSystem
) -> list[Tuple[int, FileMetaData]]:
    return sorted(
        file_infos,
        key=lambda file_info: rating_system.file_score(file_info[1]),
        reverse=True,
    )


def delete_existing_sort_tags_if_needed(client: hydrus_api.Client) -> None:
    response = client.search_files(tags=["TagRankSort:*"])
    if response is None or response["file_ids"] is None:
        print("I was not able to search for files or something went wrong when trying to.")
        print("Please check your permissions with the following help text.")
        print("If this does not help please report this error.")
        print_permissions_error_then_exit(None)
    if len(response["file_ids"]) == 0:
        return
    print("You still have files with the TagRankSort tags from an earlier sort attempt!")
    still_has_tags_response = get_file_infos_from_client(client, response["file_ids"])
    for (file_id, metadata) in still_has_tags_response:
        for (tag_repo_identifier, tag_repo_data) in metadata["tags"].items():
            if "0" not in tag_repo_data["display_tags"]:
                continue
            previous_sort_tags = [tag for tag in tag_repo_data["display_tags"]["0"] if tag.startswith("TagRankSort:")]
            if len(previous_sort_tags) > 0:
                client.add_tags(file_ids=[file_id], service_keys_to_actions_to_tags={
                    tag_repo_identifier: {hydrus_api.TagAction.DELETE: previous_sort_tags}})
    print("Existing sort tags deleted.")


GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE = 1000


def get_file_infos_from_client(client: hydrus_api.Client, file_ids: list[int]) -> list[Tuple[int, FileMetaData]]:
    file_ids_to_tags: list[Tuple[int, FileMetaData]] = []

    def get_and_process_one_chunk(chunk_of_ids: list[int]):
        file_infos_response = client.get_file_metadata(file_ids=chunk_of_ids)
        if file_infos_response is None or file_infos_response["metadata"] is None:
            print_could_not_fetch_file_information_then_exit()
        file_ids_to_tags.extend((info["file_id"], info) for info in file_infos_response["metadata"])

    if len(file_ids) < GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE:
        get_and_process_one_chunk(file_ids)
        return file_ids_to_tags

    chunks = math.ceil(len(file_ids) / GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE)
    print(f"Getting file info from the client in {chunks} chunks.")
    print("Chunks done: 0", end="")
    for (index, id_batch) in enumerate(batched(file_ids, GET_FILE_INFO_FROM_CLIENT_CHUNK_SIZE), start=1):
        get_and_process_one_chunk(id_batch)
        print(f"\rChunks done: {index}", end="", flush=True)
    print("\rChunks done: ALL")
    return file_ids_to_tags


def run_for_create_image_ranking(client: hydrus_api.Client) -> None:
    if hydrus_api.Permission.ADD_TAGS not in client.verify_access_key()["basic_permissions"]:
        print_add_tags_permissions_missing_info_then_exit()
    delete_existing_sort_tags_if_needed(client)
    rating_system = RatingSystem(client, [])
    tags = list(rating_system.current_ratings.keys())
    # noinspection PyTypeChecker
    response = client.search_files(tags=[tags])
    if response is None or response["file_ids"] is None or len(response["file_ids"]) == 0:
        print_no_relevant_files_to_sort_then_exit()
    file_ids = [int(file_id) for file_id in response["file_ids"]]
    print(f"Found {len(file_ids)} files that have at least one ranked tag.")
    file_infos = get_file_infos_from_client(client, file_ids)
    print("Got metadata and direct MMR ratings for each file from the client.")
    print("Now sorting the list by tagrankMMR...")
    sorted_file_infos = sort_files_by_mmr(file_infos, rating_system)
    print("Sorted the list. Now setting the sort-order tags in hydrus.")
    services_response = client.get_services()
    services_map = services_response["services"]
    found_service_id = None
    for service_id, service_data in services_map.items():
        if service_data["type"] == hydrus_api.ServiceType.TAG_DOMAIN:
            if found_service_id is None:
                found_service_id = service_id
            if service_data["name"] == "my tags":
                found_service_id = service_id
    for (index, (file_id, _)) in enumerate(sorted_file_infos):
        client.add_tags(file_ids=[file_id], service_keys_to_tags={found_service_id: [f"TagRankSort:{index}"]})
    print("Have sent all the tags to the client.")
    print("DONE! If you need info on how to use this to sort your files, read below:")
    print("  You can use this sort order by clicking the 'sort by(...)' button on the top left of a file search column. ")
    print("  Here, select Namespaces -> Custom. Then fill in 'TagRankSort'. Press ok, select 'display tags'.")
    print("  If you want to make this easier, go to: file -> options -> sort/collect.")
    print("  In the 'namespace file sorting' section press 'add' at the bottom.")
    print("  Fill in 'TagRankSort', press ok, then select 'display tags'.")
    print("  Press apply to save these settings.")
    print("  Now, if you want to set this as the default sort: go to: file -> options -> sort/collect.")
    print("  Click the first button to the right of the text 'Default File Sort'")
    print("  Here, select Namespaces, and click the 'sort by tags: TagRankSort' option that you just created.")
    print()
    input("Press Enter to exit...")


def main(mode: str) -> None:
    ensure_config_files()
    client = create_client_or_exit()
    if mode == MODE_RANK_TAGS:
        run_for_rank_tags(client)
    elif mode == MODE_CREATE_IMAGE_RANKING:
        run_for_create_image_ranking(client)
    else:
        print("ERROR: Unknown run mode!")


MODE_CREATE_IMAGE_RANKING = "create_image_ranking"
MODE_RANK_TAGS = "rank_tags"

if __name__ == "__main__":
    arguments = sys.argv
    mode = MODE_CREATE_IMAGE_RANKING if "--create_image_ranking" in arguments else MODE_RANK_TAGS
    main(mode)
