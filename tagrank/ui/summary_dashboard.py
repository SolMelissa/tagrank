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
