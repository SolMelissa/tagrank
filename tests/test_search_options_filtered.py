import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tagrank import pool
from tagrank.errors import UnknownServiceKeyError
from tagrank.server import app


class FakeFilterClient:
    """Minimal Hydrus client stub for build_filtered_search_options: tag -> file_ids search,
    optional per-file metadata (for aspect_ratio/pixel_count/rating_count post-filtering), and
    a fixed known-services map for service-key validation."""

    def __init__(self, files_by_tag: dict[str, list[int]], metadata_by_id: dict[int, dict] | None = None):
        self.files_by_tag = files_by_tag
        self.metadata_by_id = metadata_by_id or {}

    def search_files(self, tags, return_file_ids=None, return_hashes=None, **kwargs):
        # first element of `tags` is always the tag predicate in these tests
        tag = tags[0]
        return {"file_ids": list(self.files_by_tag.get(tag, []))}

    def get_file_metadata(self, file_ids):
        return {"metadata": [self.metadata_by_id[fid] for fid in file_ids if fid in self.metadata_by_id]}

    def get_services(self):
        return {"services": {"known-file-service": {"type": 0}, "known-tag-service": {"type": 5}}}


def _wide_open_filters(**overrides) -> pool.FilterParams:
    return pool.FilterParams(**overrides)


class BuildFilteredSearchOptionsTests(unittest.TestCase):
    def test_happy_path_ranks_and_counts_filtered_files(self):
        ratings = {"character:mario": (30.0, 1.0), "character:luigi": (10.0, 1.0)}
        client = FakeFilterClient(files_by_tag={"character:mario": [1, 2, 3], "character:luigi": [4]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(client, _wide_open_filters())

        tags = {o.tag: o for o in options.top}
        self.assertIn("character:mario", tags)
        self.assertEqual(tags["character:mario"].file_count, 3)

    def test_empty_result_after_filtering_is_not_an_error(self):
        ratings = {"character:mario": (30.0, 1.0)}
        client = FakeFilterClient(files_by_tag={})  # tag matches nothing

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(client, _wide_open_filters(min_files=1))

        self.assertEqual(options.top, [])
        self.assertEqual(options.random, [])
        self.assertEqual(options.bottom, [])

    def test_min_files_excludes_tags_below_threshold(self):
        ratings = {"character:mario": (30.0, 1.0), "character:luigi": (10.0, 1.0)}
        client = FakeFilterClient(files_by_tag={"character:mario": [1, 2], "character:luigi": [3]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(client, _wide_open_filters(min_files=2))

        all_tags = {o.tag for o in options.top + options.random + options.bottom}
        self.assertIn("character:mario", all_tags)
        self.assertNotIn("character:luigi", all_tags)

    def test_filter_tag_substring_match(self):
        ratings = {"character:mario": (30.0, 1.0), "series:mushroom_kingdom": (10.0, 1.0)}
        client = FakeFilterClient(files_by_tag={"character:mario": [1], "series:mushroom_kingdom": [2]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(client, _wide_open_filters(filter_tag="mario"))

        all_tags = {o.tag for o in options.top + options.random + options.bottom}
        self.assertEqual(all_tags, {"character:mario"})

    def test_score_band_excludes_out_of_range_tags(self):
        ratings = {"character:mario": (30.0, 1.0), "character:luigi": (-30.0, 1.0)}
        client = FakeFilterClient(files_by_tag={"character:mario": [1], "character:luigi": [2]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(client, _wide_open_filters(score_min=0.0, score_max=100.0))

        all_tags = {o.tag for o in options.top + options.random + options.bottom}
        self.assertEqual(all_tags, {"character:mario"})

    def test_aspect_ratio_and_pixel_count_post_filter_uses_metadata(self):
        ratings = {"character:mario": (30.0, 1.0)}
        # file 1: 1000x1000 (square, 1e6 px) passes; file 2: 2000x500 (4.0 ratio) fails aspect ratio band
        metadata_by_id = {
            1: {"file_id": 1, "width": 1000, "height": 1000, "tags": {}},
            2: {"file_id": 2, "width": 2000, "height": 500, "tags": {}},
        }
        client = FakeFilterClient(files_by_tag={"character:mario": [1, 2]}, metadata_by_id=metadata_by_id)

        with patch.object(pool, "load_ratings", return_value=ratings):
            options = pool.build_filtered_search_options(
                client, _wide_open_filters(aspect_ratio_min=0.5, aspect_ratio_max=1.5),
            )

        tags = {o.tag: o for o in options.top}
        self.assertEqual(tags["character:mario"].file_count, 1)

    def test_unknown_file_service_key_raises(self):
        ratings = {"character:mario": (30.0, 1.0)}
        client = FakeFilterClient(files_by_tag={"character:mario": [1]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            with self.assertRaises(UnknownServiceKeyError):
                pool.build_filtered_search_options(
                    client, _wide_open_filters(file_service_keys=["not-a-real-service"]),
                )

    def test_namespace_mode_forwarded_as_system_predicate(self):
        ratings = {"character:mario": (30.0, 1.0)}
        seen_predicates = {}

        class RecordingClient(FakeFilterClient):
            def search_files(self, tags, return_file_ids=None, return_hashes=None, **kwargs):
                seen_predicates["predicates"] = list(tags)
                return super().search_files(tags, return_file_ids, return_hashes, **kwargs)

        client = RecordingClient(files_by_tag={"character:mario": [1]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            pool.build_filtered_search_options(client, _wide_open_filters(namespace_mode="namespaced"))

        self.assertIn("system:has namespace", seen_predicates["predicates"])

    def test_archive_mode_forwarded_as_system_predicate(self):
        ratings = {"character:mario": (30.0, 1.0)}
        seen_predicates = {}

        class RecordingClient(FakeFilterClient):
            def search_files(self, tags, return_file_ids=None, return_hashes=None, **kwargs):
                seen_predicates["predicates"] = list(tags)
                return super().search_files(tags, return_file_ids, return_hashes, **kwargs)

        client = RecordingClient(files_by_tag={"character:mario": [1]})

        with patch.object(pool, "load_ratings", return_value=ratings):
            pool.build_filtered_search_options(client, _wide_open_filters(archive_mode="inbox"))

        self.assertIn("system:inbox", seen_predicates["predicates"])


class FilteredSearchOptionsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_happy_path_returns_200_with_shape_matching_search_options(self):
        fake_options = pool.SearchOptions(
            top=[pool.TagOption(index=1, tag="character:mario", score=1.8, file_count=3)],
            random=[],
            bottom=[],
            lookup={1: "character:mario"},
        )
        with patch("tagrank.service.get_filtered_search_options", return_value=fake_options):
            response = self.client.post("/search-options/filtered", json={"filter_tag": "", "min_files": 0})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body.keys()), {"top", "random", "bottom"})
        self.assertEqual(body["top"][0]["tag"], "character:mario")

    def test_empty_result_is_200_not_error(self):
        empty_options = pool.SearchOptions(top=[], random=[], bottom=[], lookup={})
        with patch("tagrank.service.get_filtered_search_options", return_value=empty_options):
            response = self.client.post("/search-options/filtered", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"top": [], "random": [], "bottom": []})

    def test_unknown_service_key_returns_400(self):
        with patch(
            "tagrank.service.get_filtered_search_options",
            side_effect=UnknownServiceKeyError("bogus-key"),
        ):
            response = self.client.post(
                "/search-options/filtered", json={"file_service_keys": ["bogus-key"]},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "UnknownServiceKeyError")

    def test_namespace_and_archive_mode_variants_accepted(self):
        empty_options = pool.SearchOptions(top=[], random=[], bottom=[], lookup={})
        with patch("tagrank.service.get_filtered_search_options", return_value=empty_options) as mocked:
            response = self.client.post(
                "/search-options/filtered",
                json={"namespace_mode": "unnamespaced", "archive_mode": "archived"},
            )

        self.assertEqual(response.status_code, 200)
        filters = mocked.call_args[0][0]
        self.assertEqual(filters.namespace_mode, "unnamespaced")
        self.assertEqual(filters.archive_mode, "archived")


if __name__ == "__main__":
    unittest.main()
