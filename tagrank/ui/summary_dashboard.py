"""Companion window that shows all session summary graphs together in one 2x2 grid, updated live."""

import matplotlib.pyplot as plt  # type: ignore
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # type: ignore
from PySide6 import QtWidgets

from tagrank.graphs import build_session_summary_figures, load_prediction_entries, top_tags_from_rating_system
from tagrank.rating import RatingSystem


class SummaryDashboard(QtWidgets.QWidget):
    def __init__(self, rating_system: RatingSystem, amount_of_tags: int):
        super().__init__()
        self.rating_system = rating_system
        self.amount_of_tags = amount_of_tags
        self.setWindowTitle("TagRank - Session Summary")
        self.resize(1100, 800)

        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(4)

        self.rising_star_label = QtWidgets.QLabel("Rising Star Feed: no movers yet this session.")
        self.rising_star_label.setWordWrap(True)
        self.rising_star_label.setStyleSheet(
            "QLabel { font-weight: bold; padding: 4px 6px; background: rgba(84, 162, 75, 0.15); "
            "border-radius: 4px; }"
        )
        outer_layout.addWidget(self.rising_star_label)

        grid_container = QtWidgets.QWidget()
        outer_layout.addWidget(grid_container, 1)
        self.grid_layout = QtWidgets.QGridLayout(grid_container)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(4)
        self.canvases: list[FigureCanvasQTAgg] = []

        self.refresh()

    def _refresh_rising_star_feed(self) -> None:
        if not self.rating_system.settings.ui.rising_star_feed_enabled:
            self.rising_star_label.hide()
            return
        self.rising_star_label.show()
        movers = self.rating_system.rising_star_feed(top_n=8)
        risers = [f"{tag} (+{delta:.1f})" for tag, delta in movers if delta > 0]
        if risers:
            self.rising_star_label.setText("\U0001F4C8 Rising Star Feed: " + "  •  ".join(risers))
        else:
            self.rising_star_label.setText("Rising Star Feed: no movers yet this session.")

    def refresh(self) -> None:
        self._refresh_rising_star_feed()

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
