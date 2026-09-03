import unittest

from tagrank.settings import PoolSettings, SearchSettings, DistanceSettings, HydrusKeys, Settings, SettingsStore, UiSettings


def _fake_settings() -> Settings:
    return Settings(
        hydrus=HydrusKeys(api_url="", api_key="", rating_service_key="", mmr_service_key="", mmr_confidence_service_key="", tag_service_key="", badge_tag_service_key=""),
        search=SearchSettings(search_query=[], default_file_query=[]),
        pool=PoolSettings(pool_size=100, candidate_seed_count=1000, seed_count_for_query=10, api_limit_fuzz=2, pool_strategy="random", max_tournament_size=64, file_service_key=""),
        distance=DistanceSettings(max_distance_start=10, distance_step=2, max_distance_hard=64, min_pool_satisfied=None),
        ui=UiSettings(top_tag_options=20, bottom_tag_options=10, random_tag_options=10, min_tag_file_count=1, amount_of_tags_in_charts=20, debug_mode=True, confidence_sigma_threshold=3.0, rising_star_feed_enabled=True, underdog_alerts_enabled=True),
    )


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        import tagrank.settings as settings_module
        self._orig_set_and_persist = settings_module.set_and_persist
        settings_module.set_and_persist = lambda name, value: None  # don't touch disk in tests
        self.store = SettingsStore(_fake_settings())

    def tearDown(self):
        import tagrank.settings as settings_module
        settings_module.set_and_persist = self._orig_set_and_persist

    def test_update_replaces_only_the_targeted_field(self):
        new_settings = self.store.update({"pool.pool_size": 250})

        self.assertEqual(new_settings.pool.pool_size, 250)
        self.assertEqual(new_settings.pool.pool_strategy, "random")  # untouched

    def test_update_swaps_current_property(self):
        self.store.update({"ui.debug_mode": False})

        self.assertFalse(self.store.current.ui.debug_mode)

    def test_unknown_key_is_ignored_without_error(self):
        new_settings = self.store.update({"pool.not_a_real_field": 1})

        self.assertEqual(new_settings.pool.pool_size, 100)

    def test_original_settings_object_is_not_mutated(self):
        original = self.store.current
        self.store.update({"pool.pool_size": 999})

        self.assertEqual(original.pool.pool_size, 100)  # frozen dataclass, old instance unaffected


if __name__ == "__main__":
    unittest.main()
