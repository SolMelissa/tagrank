import unittest

import config
from config import key
from tagrank.hydrus_client import sort_files_by_mmr
from tagrank.rating import RatingSystem, format_comparison_label, tag_confidence
from trueskill import Rating

from tests.conftest import FakeClient, metadata, tagged_metadata


class DirectFileRatingTests(unittest.TestCase):
    def test_pair_selection_handles_too_few_files(self):
        system = RatingSystem(FakeClient(), [1])

        self.assertIsNone(system.get_file_pair())

    def test_reversed_file_pairs_are_not_repeated(self):
        system = RatingSystem(FakeClient(), [1, 2])
        system.convert_image_ids_to_file_meta_data = lambda pair: pair

        first = system.get_file_pair()
        second = system.get_file_pair()

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_sorting_uses_direct_file_mmr(self):
        system = RatingSystem(FakeClient(), [])
        files = [(1, metadata(1, 5)), (2, metadata(2, 30)), (3, metadata(3))]

        self.assertEqual([file_id for file_id, _ in sort_files_by_mmr(files, system)], [2, 1, 3])

    def test_tag_confidence_is_the_conservative_tag_score(self):
        system = RatingSystem(FakeClient(), [])
        system.current_ratings["liked"] = Rating(mu=30, sigma=2)

        self.assertEqual(tag_confidence("liked", system), 2400)

    def test_tag_evidence_and_direct_file_rating_are_updated_together(self):
        client = FakeClient()
        system = RatingSystem(client, [])
        winner = tagged_metadata(1, ["liked", "shared"], 10)
        loser = tagged_metadata(2, ["disliked", "shared"], 10)

        system.process_result(winner=winner, loser=loser)

        self.assertGreater(tag_confidence("liked", system), tag_confidence("disliked", system))
        self.assertGreater(system.file_score(winner), system.file_score(loser))

    def test_shared_tags_are_not_updated_as_both_winner_and_loser(self):
        system = RatingSystem(FakeClient(), [])
        winner = tagged_metadata(1, ["liked", "shared"], 10)
        loser = tagged_metadata(2, ["disliked", "shared"], 10)

        system.process_result(winner=winner, loser=loser)

        self.assertNotIn("shared", system.go_back_ratings_stack[-1])

    def test_hydrus_mmr_is_used_as_file_score(self):
        system = RatingSystem(FakeClient(), [])

        self.assertAlmostEqual(system.file_score(metadata(1, 42)), 42)
        self.assertEqual(system.file_score(metadata(2)), 0)
        self.assertEqual(system.file_score(metadata(3, "nan")), 0)

    def test_comparison_updates_and_persists_direct_file_ratings(self):
        client = FakeClient()
        system = RatingSystem(client, [])
        winner = metadata(1, 10)
        loser = metadata(2, 10)

        system.process_result(winner=winner, loser=loser)

        self.assertGreater(system.file_score(winner), system.file_score(loser))
        self.assertEqual(len(client.writes), 4)
        self.assertTrue(any(write[0] == key("TAGRANK_MMR_SERVICE_KEY") for write in client.writes))
        self.assertTrue(any(write[0] == key("TAGRANK_MMR_CONFIDENCE_SERVICE_KEY") for write in client.writes))

    def test_undo_restores_direct_file_ratings(self):
        system = RatingSystem(FakeClient(), [])
        winner = metadata(1, 10)
        loser = metadata(2, 10)
        before = (system.file_score(winner), system.file_score(loser))

        system.process_result(winner=winner, loser=loser)
        system.process_undo()

        self.assertEqual((system.file_score(winner), system.file_score(loser)), before)

    def test_photo_confidence_is_written_to_the_configured_service_key(self):
        original_keys = config._KEYS
        try:
            config._KEYS = {
                "TAGRANK_MMR_SERVICE_KEY": "mmr-key",
                "TAGRANK_MMR_CONFIDENCE_SERVICE_KEY": "photo-confidence-key",
            }
            client = FakeClient()
            system = RatingSystem(client, [])
            winner = {"file_id": 1, "hash": "hash-1", "ratings": {"mmr-key": 10}, "tags": {}}
            loser = {"file_id": 2, "hash": "hash-2", "ratings": {"mmr-key": 10}, "tags": {}}

            system.process_result(winner=winner, loser=loser)

            confidence_writes = [write for write in client.writes if write[0] == "photo-confidence-key"]
            score_writes = [write for write in client.writes if write[0] == "mmr-key"]
            self.assertTrue(confidence_writes)
            # Confidence is the secondary TrueSkill stat (derived from sigma), not the rating itself.
            self.assertNotEqual(confidence_writes[-1][1], score_writes[-1][1])
            self.assertGreater(confidence_writes[-1][1], 0)
        finally:
            config._KEYS = original_keys

    def test_file_confidence_rises_as_comparisons_reduce_uncertainty(self):
        system = RatingSystem(FakeClient(), [])
        winner = metadata(1, 10)
        loser = metadata(2, 10)
        confidence_before = system.file_confidence(winner)

        system.process_result(winner=winner, loser=loser)

        self.assertGreater(system.file_confidence(winner), confidence_before)

    def test_comparison_label_formats_photo_and_tag_mmr(self):
        formatted = format_comparison_label(10.0, 5.0, 7.0, 3.0)

        self.assertIn("Photo MMR", formatted)
        self.assertIn("10.00 ⟵ 5.00", formatted)
        self.assertIn("Tag MMR", formatted)
        self.assertIn("7.00 ⟵ 3.00", formatted)
        self.assertIn("Likely Winner", formatted)
        self.assertIn("font-size: 24pt", formatted)
        self.assertIn("⟵", formatted)

    def test_comparison_label_uses_combined_photo_and_tag_scores_for_likely_winner(self):
        formatted = format_comparison_label(2.0, 8.0, 8.0, 0.0)

        self.assertIn("⟵", formatted)
        self.assertIn("Likely Winner", formatted)

    def test_file_score_prefers_mmr_service_when_confidence_key_is_configured(self):
        original_keys = config._KEYS
        try:
            config._KEYS = {
                "TAGRANK_MMR_SERVICE_KEY": "mmr-key",
                "TAGRANK_MMR_CONFIDENCE_SERVICE_KEY": "photo-confidence-key",
            }
            system = RatingSystem(FakeClient(), [])
            metadata_with_mmr = {"file_id": 1, "hash": "hash-1", "ratings": {"mmr-key": 42}, "tags": {}}

            self.assertAlmostEqual(system.file_score(metadata_with_mmr), 42)
        finally:
            config._KEYS = original_keys

    def test_prediction_log_tracks_raw_scores_and_confidence(self):
        system = RatingSystem(FakeClient(), [])
        left = tagged_metadata(1, ["liked", "bright"], 10)
        right = tagged_metadata(2, ["disliked"], 5)

        system.current_ratings["liked"] = Rating(mu=25, sigma=2)
        system.current_ratings["bright"] = Rating(mu=20, sigma=3)
        system.current_ratings["disliked"] = Rating(mu=10, sigma=3)

        entry = system.build_prediction_entry(left, right, "A")

        self.assertEqual(entry["user_selection"], "A")
        self.assertEqual(entry["tag_prediction"], "A")
        self.assertEqual(entry["photo_prediction"], "A")
        self.assertIn("date", entry)
        self.assertIn("time", entry)
        self.assertGreaterEqual(entry["confidence"], 0.0)
        self.assertLessEqual(entry["confidence"], 1.0)
        self.assertIn("tag_gap", entry)
        self.assertIn("photo_gap", entry)


if __name__ == "__main__":
    unittest.main()
