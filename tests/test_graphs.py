import unittest

from tagrank.graphs import build_session_summary_figures, calculate_tag_count_for_height
from trueskill import Rating


class GraphBuildingTests(unittest.TestCase):
    def test_adaptive_tag_count_scaling_uses_available_space(self):
        tall_count = calculate_tag_count_for_height(1200)
        short_count = calculate_tag_count_for_height(500)

        self.assertGreater(tall_count, short_count)
        self.assertGreaterEqual(tall_count, 5)
        self.assertLessEqual(tall_count, 20)

    def test_session_summary_figures_are_separate_and_labeled(self):
        entries = [
            {"date": "2024-01-01", "user_selection": "A", "tag_prediction": "A", "photo_prediction": "A", "confidence": 0.9},
            {"date": "2024-01-01", "user_selection": "B", "tag_prediction": "A", "photo_prediction": "B", "confidence": 0.3},
            {"date": "2024-01-02", "user_selection": "A", "tag_prediction": "A", "photo_prediction": "A", "confidence": 0.8},
        ]
        top_tags = [("tag_a", Rating(mu=20, sigma=2)), ("tag_b", Rating(mu=18, sigma=2)), ("tag_c", Rating(mu=16, sigma=2))]

        figures = build_session_summary_figures(entries, top_tags, figure_height=700)

        self.assertEqual(len(figures), 4)
        self.assertTrue(all(hasattr(fig, "axes") for fig in figures))
        self.assertTrue(all(len(fig.axes) >= 1 for fig in figures))


if __name__ == "__main__":
    unittest.main()
