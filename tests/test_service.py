import unittest
from unittest.mock import patch

from tagrank import service
from tagrank.errors import NoRelevantFilesError
from tagrank.rating import RatingSystem
from tagrank.service import NoPairAvailableError, Session, SessionNotFoundError

from tests.conftest import FakeClient, tagged_metadata


def _make_session(file_ids=(1, 2)) -> Session:
    client = FakeClient()
    rating_system = RatingSystem(client, list(file_ids))
    session = Session(id="test-session", rating_system=rating_system, client=client)
    service._sessions[session.id] = session
    return session


class SessionLifecycleTests(unittest.TestCase):
    def tearDown(self):
        service._sessions.clear()

    def test_get_session_raises_for_unknown_id(self):
        with self.assertRaises(SessionNotFoundError):
            service.get_session("does-not-exist")

    def test_get_session_returns_registered_session(self):
        session = _make_session()

        self.assertIs(service.get_session(session.id), session)

    def test_end_session_persists_and_drops_it(self):
        session = _make_session()
        with patch.object(session.rating_system, "write_results_to_file") as write_mock:
            service.end_session(session)

        write_mock.assert_called_once()
        with self.assertRaises(SessionNotFoundError):
            service.get_session(session.id)


class ComparisonFlowTests(unittest.TestCase):
    def tearDown(self):
        service._sessions.clear()

    def test_submit_result_without_a_pending_pair_raises(self):
        session = _make_session()

        with self.assertRaises(NoPairAvailableError):
            service.submit_result(session, "left")

    def test_submit_result_rejects_unknown_choice(self):
        session = _make_session()
        session.rating_system.convert_image_ids_to_file_meta_data = lambda pair: (
            tagged_metadata(pair[0], ["a"]), tagged_metadata(pair[1], ["b"]),
        )
        service.get_next_pair(session)

        with self.assertRaises(ValueError):
            service.submit_result(session, "up")

    def test_full_pair_and_submit_flow_updates_ratings_and_writes_choices(self):
        session = _make_session()
        session.rating_system.convert_image_ids_to_file_meta_data = lambda pair: (
            tagged_metadata(pair[0], ["liked"]), tagged_metadata(pair[1], ["disliked"]),
        )

        pair = service.get_next_pair(session)
        self.assertIsNotNone(pair)
        left, right = session.left, session.right

        with patch.object(session.rating_system, "write_prediction_log_entry") as log_mock:
            service.submit_result(session, "left")

        log_mock.assert_called_once_with(left, right, "A")
        # winner/loser ratings pushed to Hydrus via write_choice -> client.set_rating, plus
        # per-file MMR rating and MMR confidence writes (2 files x 3 services = 6 writes).
        self.assertEqual(len(session.client.writes), 6)
        # the pending pair is cleared, so a second submit without a new next-pair call fails
        with self.assertRaises(NoPairAvailableError):
            service.submit_result(session, "left")

    def test_undo_delegates_to_rating_system(self):
        session = _make_session()
        with patch.object(session.rating_system, "process_undo") as undo_mock:
            service.undo(session)

        undo_mock.assert_called_once()


class DataQueryTests(unittest.TestCase):
    def test_list_tags_is_sorted_strongest_first(self):
        with patch("tagrank.pool.load_ratings", return_value={"low": (10.0, 5.0), "high": (30.0, 1.0)}):
            tags = service.list_tags()

        self.assertEqual([entry["tag"] for entry in tags], ["high", "low"])

    def test_get_prediction_history_delegates_to_graphs_module(self):
        with patch("tagrank.graphs.load_prediction_entries", return_value=[{"date": "2024-01-01"}]) as load_mock:
            history = service.get_prediction_history()

        load_mock.assert_called_once()
        self.assertEqual(history, [{"date": "2024-01-01"}])

    def test_start_session_raises_when_pool_is_empty(self):
        with patch("tagrank.pool.build_pool", return_value=[]):
            with self.assertRaises(NoRelevantFilesError):
                service.start_session(query=["nonexistent:tag"], client=FakeClient())


if __name__ == "__main__":
    unittest.main()
