"""Settings dialog reachable from the main window's Settings menu - the numeric-settings
half of the settings panel (frequent toggles live directly on the menu bar/toolbar instead,
see tagrank/ui/window.py's _build_menu_bar). Edits go through SettingsStore (rebuild-and-
swap, see tagrank/settings.py) so they persist to config/SETTINGS and take effect live."""

from __future__ import annotations

from PySide6 import QtWidgets

from tagrank.settings import Settings


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TagRank Settings")
        self._settings = settings
        self._fields: dict[str, QtWidgets.QWidget] = {}

        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._pool_tab(), "Pool")
        tabs.addTab(self._distance_tab(), "Distance")
        tabs.addTab(self._ui_tab(), "UI / Display")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _spin(self, path: str, value: int, minimum: int = 0, maximum: int = 1_000_000) -> QtWidgets.QSpinBox:
        box = QtWidgets.QSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        self._fields[path] = box
        return box

    def _double_spin(self, path: str, value: float, minimum: float = 0.0, maximum: float = 100.0) -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setDecimals(2)
        box.setValue(value)
        self._fields[path] = box
        return box

    def _pool_tab(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(widget)
        pool = self._settings.pool
        form.addRow("Pool size", self._spin("pool.pool_size", pool.pool_size, 2, 20000))
        form.addRow("Candidate seed count", self._spin("pool.candidate_seed_count", pool.candidate_seed_count, 1, 200000))
        form.addRow("Seed count for query", self._spin("pool.seed_count_for_query", pool.seed_count_for_query, 1, 5000))
        form.addRow("API limit fuzz", self._spin("pool.api_limit_fuzz", pool.api_limit_fuzz, 1, 20))
        form.addRow("Max tournament size", self._spin("pool.max_tournament_size", pool.max_tournament_size, 2, 1024))
        return widget

    def _distance_tab(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(widget)
        distance = self._settings.distance
        form.addRow("Max distance start", self._spin("distance.max_distance_start", distance.max_distance_start, 0, 128))
        form.addRow("Distance step", self._spin("distance.distance_step", distance.distance_step, 1, 32))
        form.addRow("Max distance hard", self._spin("distance.max_distance_hard", distance.max_distance_hard, 1, 128))
        form.addRow(
            "Min pool satisfied (%)",
            self._double_spin("distance.min_pool_satisfied", distance.min_pool_satisfied or 100.0, 0.0, 100.0),
        )
        return widget

    def _ui_tab(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(widget)
        ui = self._settings.ui
        form.addRow("Top tag options", self._spin("ui.top_tag_options", ui.top_tag_options, 0, 200))
        form.addRow("Bottom tag options", self._spin("ui.bottom_tag_options", ui.bottom_tag_options, 0, 200))
        form.addRow("Random tag options", self._spin("ui.random_tag_options", ui.random_tag_options, 0, 200))
        form.addRow("Min tag file count", self._spin("ui.min_tag_file_count", ui.min_tag_file_count, 0, 10000))
        form.addRow("Tags shown in charts", self._spin("ui.amount_of_tags_in_charts", ui.amount_of_tags_in_charts, 5, 500))
        form.addRow(
            "Confidence sigma threshold",
            self._double_spin("ui.confidence_sigma_threshold", ui.confidence_sigma_threshold, 0.0, 20.0),
        )
        debug_box = QtWidgets.QCheckBox()
        debug_box.setChecked(ui.debug_mode)
        self._fields["ui.debug_mode"] = debug_box
        form.addRow("Debug mode", debug_box)
        return widget

    def changes(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for path, widget in self._fields.items():
            if isinstance(widget, QtWidgets.QCheckBox):
                result[path] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                result[path] = widget.value()
            elif isinstance(widget, QtWidgets.QSpinBox):
                result[path] = widget.value()
        return result
