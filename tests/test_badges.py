import unittest

from tagrank import badges


class BadgeEngineTests(unittest.TestCase):
    def setUp(self):
        # Isolate badges/stats storage per test instead of touching real data/*.json.
        self._orig_stats_path = badges.STATS_PATH
        self._orig_badges_path = badges.BADGES_PATH
        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        badges.STATS_PATH = Path(self._tmpdir.name) / "badge_stats.json"
        badges.BADGES_PATH = Path(self._tmpdir.name) / "badges.json"

    def tearDown(self):
        badges.STATS_PATH = self._orig_stats_path
        badges.BADGES_PATH = self._orig_badges_path
        self._tmpdir.cleanup()

    def test_first_win_earns_first_blood(self):
        earned = badges.record_result("tag", "cats", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)

        self.assertIn("first_blood", {b.id for b in earned})

    def test_badges_are_permanent_once_earned(self):
        badges.record_result("tag", "cats", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)
        # A later loss should not remove the earlier badge.
        badges.record_result("tag", "cats", won=False, mu=24.0, sigma=8.0, confidence_threshold=3.0)

        self.assertIn("first_blood", badges.held_badge_ids("tag", "cats"))

    def test_win_streak_badge_requires_five_in_a_row(self):
        for _ in range(4):
            earned = badges.record_result("tag", "dogs", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)
            self.assertNotIn("on_a_roll", {b.id for b in earned})
        earned = badges.record_result("tag", "dogs", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)

        self.assertIn("on_a_roll", {b.id for b in earned})

    def test_streak_resets_on_a_loss(self):
        for _ in range(4):
            badges.record_result("tag", "birds", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)
        badges.record_result("tag", "birds", won=False, mu=24.0, sigma=8.0, confidence_threshold=3.0)
        earned = badges.record_result("tag", "birds", won=True, mu=26.0, sigma=8.0, confidence_threshold=3.0)

        self.assertNotIn("on_a_roll", {b.id for b in earned})

    def test_entity_scoping_picture_win_does_not_award_tag_badge(self):
        """The core scoping rule: recording a picture result must never touch tag stats/badges,
        and vice versa - each entity type is evaluated in total isolation."""
        badges.record_result(
            "picture", "hash-1", won=True, mu=40.0, sigma=1.0, confidence_threshold=3.0,
            upset_sigma_multiple=5.0,
        )

        self.assertEqual(badges.held_badge_ids("tag", "hash-1"), set())
        self.assertIn("dark_horse", badges.held_badge_ids("picture", "hash-1"))

    def test_underdog_requires_sigma_multiple_at_or_above_threshold(self):
        earned_low = badges.record_result(
            "picture", "hash-a", won=True, mu=30.0, sigma=2.0, confidence_threshold=3.0,
            upset_sigma_multiple=2.9,
        )
        earned_high = badges.record_result(
            "picture", "hash-b", won=True, mu=30.0, sigma=2.0, confidence_threshold=3.0,
            upset_sigma_multiple=3.0,
        )

        self.assertNotIn("dark_horse", {b.id for b in earned_low})
        self.assertIn("dark_horse", {b.id for b in earned_high})

    def test_all_badge_icons_exist_and_are_unique_files(self):
        paths = [badges.icon_path(bid) for bid in badges.BADGE_BY_ID]

        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            self.assertTrue(path.is_file(), f"missing icon file: {path}")

    def test_exactly_sixty_badges_thirty_each(self):
        self.assertEqual(len(badges.TAG_BADGES), 30)
        self.assertEqual(len(badges.PICTURE_BADGES), 30)
        self.assertEqual(len(badges.BADGE_BY_ID), 60)


if __name__ == "__main__":
    unittest.main()
