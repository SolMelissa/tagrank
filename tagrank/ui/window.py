"""The comparison Window widget: shows two candidate files side by side."""

import contextlib
import os
from typing import Tuple

import hydrus_api  # type: ignore
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import Qt

from tagrank.rating import FileMetaData, RatingSystem, average_tag_confidence, file_tag_text, format_comparison_label
from tagrank_pool import write_choice


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
