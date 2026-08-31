"""The comparison Window widget: shows two candidate files side by side."""

import contextlib
import os
from typing import Callable

import hydrus_api  # type: ignore
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import Qt

from tagrank import badges, tournament as tournament_module
from tagrank.presets import load_presets
from tagrank.rating import FileMetaData, RatingSystem, average_tag_confidence, file_tag_text, format_comparison_label
from tagrank.pool import write_choice
from tagrank.settings import POOL_STRATEGIES, get_settings_store
from tagrank.ui.settings_dialog import SettingsDialog


class Window(QtWidgets.QMainWindow):
    def __init__(
        self,
        rating_system: RatingSystem,
        client: hydrus_api.Client,
        on_change: Callable[[], None] | None = None,
        dashboard: QtWidgets.QWidget | None = None,
    ):
        super().__init__()
        self.client = client
        self.left_file_metadata: FileMetaData = {}
        self.right_file_metadata: FileMetaData = {}
        self.rating_system: RatingSystem = rating_system
        self.on_change = on_change
        self.dashboard = dashboard
        self.go_back_image_pairs_stack: list[tuple[int, int]] = []
        self.comparisons = 0
        self._last_scaled_pair: tuple[int, int] | None = None
        self.active_tournament: tournament_module.Tournament | None = None
        self._pending_tournament_match: tournament_module.Match | None = None
        self.restart_preset_id: str | None = None
        self.set_window_title_based_on_comparison_count()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        self.main_layout = QtWidgets.QHBoxLayout(central)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(8)

        self._build_menu_bar()

        self._toast_style = (
            "QLabel { background: rgba(30,30,30,230); color: gold; border: 1px solid gold; "
            "border-radius: 6px; padding: 6px 10px; font-weight: bold; }"
        )
        self.badge_toast = self._make_toast_label()
        self.left_toast = self._make_toast_label()
        self.right_toast = self._make_toast_label()

        self.left_badge_row = QtWidgets.QWidget()
        self.right_badge_row = QtWidgets.QWidget()
        self.left_badge_layout = QtWidgets.QHBoxLayout(self.left_badge_row)
        self.right_badge_layout = QtWidgets.QHBoxLayout(self.right_badge_row)
        for badge_layout in (self.left_badge_layout, self.right_badge_layout):
            badge_layout.setContentsMargins(0, 2, 0, 2)
            badge_layout.setSpacing(4)
            badge_layout.addStretch(1)
            badge_layout.addStretch(1)

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
        left_column = QtWidgets.QVBoxLayout()
        left_column.addWidget(self.leftImageLabel, 1)
        left_column.addWidget(self.left_badge_row, 0)
        right_column = QtWidgets.QVBoxLayout()
        right_column.addWidget(self.rightImageLabel, 1)
        right_column.addWidget(self.right_badge_row, 0)
        self.image_layout.addLayout(left_column, 1)
        self.image_layout.addLayout(right_column, 1)
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

        self.store_metadata_and_show_images_for_comparison_pair(self._next_pair())

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        search_menu = menu_bar.addMenu("&Search")
        presets_menu = search_menu.addMenu("Mood/Theme Presets")
        for preset in load_presets():
            action = presets_menu.addAction(preset.name)
            action.setToolTip(preset.description)
            action.setStatusTip(preset.description)
            action.triggered.connect(lambda checked=False, p=preset: self._show_preset_info(p))

        pool_menu = menu_bar.addMenu("&Pool")
        strategy_menu = pool_menu.addMenu("Pairing Strategy")
        strategy_group = QtGui.QActionGroup(self)
        strategy_group.setExclusive(True)
        current_strategy = self.rating_system.settings.pool.pool_strategy
        for strategy in POOL_STRATEGIES:
            action = strategy_menu.addAction(strategy.replace("_", " ").title())
            action.setCheckable(True)
            action.setChecked(strategy == current_strategy)
            action.triggered.connect(lambda checked=False, s=strategy: self._set_pool_strategy(s))
            strategy_group.addAction(action)
        pool_menu.addSeparator()
        tournament_action = pool_menu.addAction("Start Tournament Mode")
        tournament_action.triggered.connect(self._start_tournament)
        rediscover_action = pool_menu.addAction("Surprise Me (Random Rediscovery)")
        rediscover_action.triggered.connect(self._rediscover)

        view_menu = menu_bar.addMenu("&View")
        self.rising_star_action = view_menu.addAction("Rising Star Feed")
        self.rising_star_action.setCheckable(True)
        self.rising_star_action.setChecked(self.rating_system.settings.ui.rising_star_feed_enabled)
        self.rising_star_action.toggled.connect(lambda on: self._set_ui_toggle("rising_star_feed_enabled", on))
        self.underdog_action = view_menu.addAction("Underdog Alerts")
        self.underdog_action.setCheckable(True)
        self.underdog_action.setChecked(self.rating_system.settings.ui.underdog_alerts_enabled)
        self.underdog_action.toggled.connect(lambda on: self._set_ui_toggle("underdog_alerts_enabled", on))
        view_menu.addSeparator()
        badges_action = view_menu.addAction("View Badges...")
        badges_action.triggered.connect(self._show_badges_dialog)

        settings_menu = menu_bar.addMenu("&Settings")
        settings_action = settings_menu.addAction("Settings...")
        settings_action.triggered.connect(self._show_settings_dialog)

        self._toolbar = self.addToolBar("Quick Actions")
        self._toolbar.addAction(tournament_action)
        self._toolbar.addAction(rediscover_action)
        self._toolbar.addAction(self.rising_star_action)
        self._toolbar.addAction(self.underdog_action)

    def _set_pool_strategy(self, strategy: str) -> None:
        store = get_settings_store()
        new_settings = store.update({"pool.pool_strategy": strategy})
        self.rating_system.settings = new_settings

    def _set_ui_toggle(self, field_name: str, value: bool) -> None:
        store = get_settings_store()
        new_settings = store.update({f"ui.{field_name}": value})
        self.rating_system.settings = new_settings

    def _show_preset_info(self, preset) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(preset.name)
        box.setText(f"{preset.description}\n\nRestart the session now using this preset?")
        restart_button = box.addButton("Restart with This Preset", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is restart_button:
            self.restart_preset_id = preset.id
            self.close()

    def _show_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.rating_system.settings, self)
        if dialog.exec():
            new_settings = get_settings_store().update(dialog.changes())
            self.rating_system.settings = new_settings

    def _show_badges_dialog(self) -> None:
        all_badges = badges.load_badges()
        lines = []
        for entity_key, label in (("tags", "Tags"), ("pictures", "Pictures")):
            bucket = all_badges.get(entity_key, {})
            if not bucket:
                continue
            lines.append(f"--- {label} ---")
            for entity_id, entries in bucket.items():
                icons = " ".join(
                    badges.BADGE_BY_ID[e["badge_id"]].icon
                    for e in entries if e["badge_id"] in badges.BADGE_BY_ID
                )
                names = ", ".join(
                    badges.BADGE_BY_ID[e["badge_id"]].name
                    for e in entries if e["badge_id"] in badges.BADGE_BY_ID
                )
                lines.append(f"{icons}  {entity_id}: {names}")
        text = "\n".join(lines) if lines else "No badges earned yet - keep comparing!"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Earned Badges")
        box.setText(text)
        box.exec()

    def _make_toast_label(self) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(self)
        label.setStyleSheet(self._toast_style)
        label.setWordWrap(True)
        label.hide()
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(label.hide)
        label._hide_timer = timer  # keep a reference alive
        return label

    def _show_toast(self, text: str, anchor: QtWidgets.QWidget | None = None) -> None:
        label = self.left_toast if anchor is self.leftImageLabel else self.right_toast if anchor is self.rightImageLabel else self.badge_toast
        label.setText(text)
        label.adjustSize()
        if anchor is not None:
            anchor_topleft = anchor.mapTo(self, QtCore.QPoint(0, 0))
            x = anchor_topleft.x() + (anchor.width() - label.width()) // 2
            y = anchor_topleft.y() + 10
        else:
            x = (self.width() - label.width()) // 2
            y = self.menuBar().height() + 10
        x = max(0, min(x, self.width() - label.width()))
        label.move(x, y)
        label.show()
        label.raise_()
        label._hide_timer.start(4000)

    def _announce_result(self, result_info: dict, winner_side: str | None = None) -> None:
        left_messages: list[str] = []
        right_messages: list[str] = []
        general_messages: list[str] = []
        winner_anchor = self.leftImageLabel if winner_side == "A" else self.rightImageLabel
        loser_anchor = self.rightImageLabel if winner_side == "A" else self.leftImageLabel

        for badge in result_info.get("winner_file_badges") or []:
            (left_messages if winner_anchor is self.leftImageLabel else right_messages).append(
                f"{badge.icon} {badge.name}!"
            )
        for badge in result_info.get("loser_file_badges") or []:
            (left_messages if loser_anchor is self.leftImageLabel else right_messages).append(
                f"{badge.icon} {badge.name}!"
            )
        for tag, earned in {**result_info.get("winner_tag_badges", {}), **result_info.get("loser_tag_badges", {})}.items():
            for badge in earned:
                general_messages.append(f"{badge.icon} '{tag}' earned {badge.name}!")
        underdog = result_info.get("underdog_alert")
        if underdog is not None:
            general_messages.append(f"\U0001F6A8 Underdog Alert! ({underdog['sigma_multiple']:.1f}σ upset)")

        if left_messages:
            self._show_toast("\n".join(left_messages), anchor=self.leftImageLabel)
        if right_messages:
            self._show_toast("\n".join(right_messages), anchor=self.rightImageLabel)
        if general_messages:
            self._show_toast("\n".join(general_messages))

    def _next_pair(self) -> tuple[FileMetaData, FileMetaData] | None:
        if self.active_tournament is not None and not self.active_tournament.is_complete:
            match = self.active_tournament.pending_match()
            self._pending_tournament_match = match
            if match is None:
                return None
            return self.rating_system.convert_image_ids_to_file_meta_data((match.left_id, match.right_id))
        self._pending_tournament_match = None
        return self.rating_system.get_file_pair()

    def _start_tournament(self) -> None:
        max_size = self.rating_system.settings.pool.max_tournament_size
        self.active_tournament = tournament_module.start_tournament(self.rating_system.file_ids, max_size)
        self._show_toast(f"\U0001F3C6 Tournament started: {len(self.active_tournament.entrants)} entrants!")
        self.store_metadata_and_show_images_for_comparison_pair(self._next_pair())

    def _rediscover(self) -> None:
        picked = self.rating_system.pick_rediscovery_file_id()
        if picked is None:
            self._show_toast("Not enough rated pictures yet for Random Rediscovery.")
            return
        opponent_candidates = [fid for fid in self.rating_system.file_ids if fid != picked]
        if not opponent_candidates:
            return
        import random
        opponent = random.choice(opponent_candidates)
        self.rating_system.inject_rediscovery_pick(picked)
        pair = self.rating_system.convert_image_ids_to_file_meta_data((picked, opponent))
        self.store_metadata_and_show_images_for_comparison_pair(pair)

    def set_window_title_based_on_comparison_count(self):
        strategy = self.rating_system.settings.pool.pool_strategy.replace("_", " ").title()
        title = f"TagRank - Comparisons done this session: {self.comparisons} | Strategy: {strategy}"
        if self.active_tournament is not None:
            title += f" | Tournament: round {self.active_tournament.current_round + 1}/{len(self.active_tournament.rounds)}"
        self.setWindowTitle(title)

    def _render_badge_strip(self, layout: QtWidgets.QHBoxLayout, entity_type: str, entity_id: str) -> None:
        """Repopulate `layout` with one icon widget per earned badge (real SVG icon from
        game-icons.net, falling back to the badge's emoji if the file can't be loaded), each
        with a tooltip showing the badge's name and description."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        layout.addStretch(1)
        for badge_id in sorted(badges.held_badge_ids(entity_type, entity_id)):
            badge = badges.BADGE_BY_ID.get(badge_id)
            if badge is None:
                continue
            icon_label = QtWidgets.QLabel()
            pixmap = QtGui.QIcon(str(badges.icon_path(badge_id))).pixmap(24, 24)
            if pixmap.isNull():
                icon_label.setText(badge.icon)
            else:
                icon_label.setPixmap(pixmap)
            icon_label.setToolTip(f"{badge.icon} {badge.name}\n{badge.description}")
            icon_label.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            layout.addWidget(icon_label)
        layout.addStretch(1)

    def refresh_comparison_details(self):
        left_tags = file_tag_text(self.left_file_metadata, self.rating_system)
        right_tags = file_tag_text(self.right_file_metadata, self.rating_system)
        self.left_tags_box.setPlainText(left_tags)
        self.right_tags_box.setPlainText(right_tags)

        left_hash = self.left_file_metadata.get("hash")
        right_hash = self.right_file_metadata.get("hash")
        self._render_badge_strip(self.left_badge_layout, "picture", left_hash or "")
        self._render_badge_strip(self.right_badge_layout, "picture", right_hash or "")

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

    def store_metadata_and_show_images_for_comparison_pair(self, metadatas: tuple[FileMetaData, FileMetaData] | None):
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
        if self.on_change is not None:
            self.on_change()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        winner_side = None
        if key == QtCore.Qt.Key.Key_Left or key == QtCore.Qt.Key.Key_A:
            winner_side = "A"
            self.rating_system.write_prediction_log_entry(self.left_file_metadata, self.right_file_metadata, "A")
            self.rating_system.process_result(winner=self.left_file_metadata, loser=self.right_file_metadata)
            write_choice(self.left_file_metadata["hash"], liked=True, client=self.client)
            write_choice(self.right_file_metadata["hash"], liked=False, client=self.client)
        elif key == QtCore.Qt.Key.Key_Right or key == QtCore.Qt.Key.Key_D:
            winner_side = "B"
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

        # Cosmetic/secondary bookkeeping (tournament progress, badge/underdog toasts) must
        # never be able to block advancing to the next pair - a bug here previously left the
        # window stuck on the same images while the comparison count kept climbing, since an
        # uncaught exception mid-keyPressEvent aborts everything after it (Qt just logs it and
        # keeps running). Isolate it so the pairing flow below always executes regardless.
        try:
            if self._pending_tournament_match is not None and self.active_tournament is not None:
                winner_meta = self.left_file_metadata if winner_side == "A" else self.right_file_metadata
                loser_meta = self.right_file_metadata if winner_side == "A" else self.left_file_metadata
                winner_id, loser_id = int(winner_meta["file_id"]), int(loser_meta["file_id"])
                self.active_tournament.bracket_id_to_hash[winner_id] = winner_meta["hash"]
                self.active_tournament.bracket_id_to_hash[loser_id] = loser_meta["hash"]
                mu_by_id = {fid: r.mu for fid, r in self.rating_system.file_ratings.items()}
                sigma_by_id = {fid: r.sigma for fid, r in self.rating_system.file_ratings.items()}
                tournament_module.check_bracket_buster(self.active_tournament, self._pending_tournament_match, winner_id, mu_by_id, sigma_by_id)
                self.active_tournament.record_winner(self._pending_tournament_match, winner_id)
                if self.active_tournament.is_complete:
                    tournament_module.finish_tournament(self.active_tournament)
                    self._show_toast(f"\U0001F3C6 Tournament complete! Champion crowned.")
                else:
                    round_matches = self.active_tournament.rounds[self.active_tournament.current_round]
                    done = sum(1 for m in round_matches if m.winner_id is not None)
                    self._show_toast(
                        f"\U0001F3C6 Round {self.active_tournament.current_round + 1}/"
                        f"{len(self.active_tournament.rounds)} - match {done}/{len(round_matches)}"
                    )

            self.set_window_title_based_on_comparison_count()
            self._announce_result(self.rating_system.last_result_info, winner_side=winner_side)
        except Exception as e:
            print(f"ERROR: tournament/badge bookkeeping failed (comparison still recorded): {e}")

        self.store_image_pair_onto_undo_stack(self.left_file_metadata, self.right_file_metadata)
        self.store_metadata_and_show_images_for_comparison_pair(self._next_pair())
        if self.on_change is not None:
            self.on_change()

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
        if self.dashboard is not None:
            self.dashboard.close()

    def prepare_to_quit(self):
        print("Saving results to file...")
        self.rating_system.write_results_to_file()
