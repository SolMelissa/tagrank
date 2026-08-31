import unittest

from tagrank import tournament


class TournamentTests(unittest.TestCase):
    def test_bracket_size_is_largest_power_of_two_within_cap(self):
        t = tournament.start_tournament(pool_ids=list(range(100)), max_size=64)

        self.assertEqual(len(t.entrants), 64)
        self.assertEqual(len(t.rounds), 6)  # log2(64)

    def test_bracket_size_capped_by_pool_when_pool_is_smaller(self):
        t = tournament.start_tournament(pool_ids=list(range(10)), max_size=64)

        self.assertEqual(len(t.entrants), 8)  # largest power of 2 <= 10

    def test_first_round_pairs_every_entrant_exactly_once(self):
        t = tournament.start_tournament(pool_ids=list(range(20)), max_size=8)

        first_round_ids = set()
        for match in t.rounds[0]:
            first_round_ids.add(match.left_id)
            first_round_ids.add(match.right_id)

        self.assertEqual(first_round_ids, set(t.entrants))

    def test_full_bracket_produces_a_single_champion(self):
        t = tournament.start_tournament(pool_ids=list(range(8)), max_size=8)

        while not t.is_complete:
            match = t.pending_match()
            self.assertIsNotNone(match)
            t.record_winner(match, match.left_id)

        self.assertIsNotNone(t.champion_id)
        self.assertIn(t.champion_id, t.entrants)

    def test_winner_advances_to_correct_next_round_slot(self):
        t = tournament.start_tournament(pool_ids=list(range(4)), max_size=4)
        first_match = t.rounds[0][0]

        t.record_winner(first_match, first_match.left_id)

        self.assertEqual(t.rounds[1][0].left_id, first_match.left_id)


if __name__ == "__main__":
    unittest.main()
