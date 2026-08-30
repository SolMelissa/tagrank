import unittest

import config
from config import key
from main import RatingSystem, sort_files_by_mmr, tag_confidence, format_comparison_label
from trueskill import Rating


class FakeClient:
    def __init__(self):
        self.writes = []

    def set_rating(self, service_key, rating, hashes=None):
        self.writes.append((service_key, rating, hashes))


def metadata(file_id, score=None):
    ratings = {} if score is None else {key("TAGRANK_MMR_SERVICE_KEY"): score}
    return {"file_id": file_id, "hash": f"hash-{file_id}", "ratings": ratings, "tags": {}}


def tagged_metadata(file_id, tags, score=None):
    return {
        **metadata(file_id, score),
        "tags": {"my tags": {"display_tags": {"0": tags}}},
    }


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
            config._KEYS = {"TAGRANK_MMR_CONFIDENCE_SERVICE_KEY": "photo-confidence-key"}
            client = FakeClient()
            system = RatingSystem(client, [])
            winner = {
                **metadata(1, 10),
                "ratings": {"photo-confidence-key": 10},
            }

            system.write_file_mmr_confidence_rating(winner)

            self.assertEqual(client.writes[-1][0], "photo-confidence-key")
            self.assertEqual(client.writes[-1][1], 10)
        finally:
            config._KEYS = original_keys

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


if __name__ == "__main__":
    unittest.main()
