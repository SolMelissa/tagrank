import unittest

from trueskill import Rating

from tagrank.rating import RatingSystem
from dataclasses import replace

from tests.conftest import FakeClient


class PairingStrategyTests(unittest.TestCase):
    def _system_with_strategy(self, strategy: str, file_ids=(1, 2, 3, 4)) -> RatingSystem:
        system = RatingSystem(FakeClient(), list(file_ids))
        system.settings = replace(system.settings, pool=replace(system.settings.pool, pool_strategy=strategy))
        system.convert_image_ids_to_file_meta_data = lambda pair: pair
        return system

    def test_confidence_duel_prefers_high_combined_sigma_low_mu_gap(self):
        system = self._system_with_strategy("confidence_duel")
        # File 1 and 2 have huge sigma overlap; 3 and 4 are far apart and confident.
        system.file_ratings[1] = Rating(mu=25, sigma=8)
        system.file_ratings[2] = Rating(mu=25, sigma=8)
        system.file_ratings[3] = Rating(mu=10, sigma=1)
        system.file_ratings[4] = Rating(mu=40, sigma=1)

        pair = system._strategy_pair("confidence_duel")

        self.assertIsNotNone(pair)
        self.assertEqual(set(pair) & {1, 2}, {1, 2})

    def test_divergence_prefers_close_mu_pairs(self):
        system = self._system_with_strategy("divergence")
        system.file_ratings[1] = Rating(mu=25, sigma=8)
        system.file_ratings[2] = Rating(mu=25.5, sigma=1)
        system.file_ratings[3] = Rating(mu=5, sigma=1)
        system.file_ratings[4] = Rating(mu=45, sigma=1)

        pair = system._strategy_pair("divergence")

        self.assertIsNotNone(pair)
        self.assertEqual(set(pair) & {1, 2}, {1, 2})

    def test_random_strategy_is_unaffected_and_still_returns_a_pair(self):
        system = self._system_with_strategy("random")

        pair = system.get_file_pair()

        self.assertIsNotNone(pair)


if __name__ == "__main__":
    unittest.main()
