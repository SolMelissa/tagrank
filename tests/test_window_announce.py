import unittest
from types import SimpleNamespace

from tagrank.badges import BADGE_BY_ID
from tagrank.ui.window import Window


class AnnounceResultTests(unittest.TestCase):
    """Regression test: winner_file_badges/loser_file_badges are a flat list[BadgeDef] (see
    RatingSystem._record_file_badges -> badges.record_result), not a nested list. Iterating
    them as if nested (`for badge in earned` over an already-a-badge item) crashes
    mid-keyPressEvent, which - because Qt swallows the exception and just logs it - silently
    aborts everything after the crash point, including advancing to the next comparison pair
    (the comparison count still increments since that happens earlier in the handler)."""

    @staticmethod
    def _fake_window(toasts):
        left_label = object()
        right_label = object()
        return SimpleNamespace(
            _show_toast=lambda text, anchor=None: toasts.append(text),
            leftImageLabel=left_label,
            rightImageLabel=right_label,
        )

    def test_flat_picture_badge_list_does_not_raise(self):
        toasts = []
        fake_self = self._fake_window(toasts)
        earned_badge = next(iter(BADGE_BY_ID.values()))
        result_info = {
            "winner_tag_badges": {},
            "loser_tag_badges": {},
            "winner_file_badges": [earned_badge],
            "loser_file_badges": [],
            "underdog_alert": None,
        }

        Window._announce_result(fake_self, result_info, winner_side="A")

        self.assertEqual(len(toasts), 1)
        self.assertIn(earned_badge.name, toasts[0])

    def test_empty_result_info_produces_no_toast(self):
        toasts = []
        fake_self = self._fake_window(toasts)

        Window._announce_result(fake_self, {})

        self.assertEqual(toasts, [])


if __name__ == "__main__":
    unittest.main()
