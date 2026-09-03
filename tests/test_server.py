import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tagrank import service
from tagrank.rating import RatingSystem
from tagrank.server import app
from tagrank.service import Session

from tests.conftest import FakeClient, tagged_metadata


def _fake_start_session(*, query=None, pool_size=None, preset_id=None, pool_strategy=None, use_similarity=True):
    client = FakeClient()
    rating_system = RatingSystem(client, [1, 2])
    rating_system.convert_image_ids_to_file_meta_data = lambda pair: (
        tagged_metadata(pair[0], ["liked"]), tagged_metadata(pair[1], ["disliked"]),
    )
    session = Session(id="server-test-session", rating_system=rating_system, client=client)
    service._sessions[session.id] = session
    return session


class ServerEndToEndTests(unittest.TestCase):
    def setUp(self):
        service._sessions.clear()
        self.client = TestClient(app)

    def tearDown(self):
        service._sessions.clear()

    def _start_session_and_wait(self) -> str:
        with patch("tagrank.service.start_session", side_effect=_fake_start_session):
            response = self.client.post("/sessions", json={"query": ["test:tag"]})
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]

            for _ in range(50):
                status = self.client.get(f"/sessions/{job_id}").json()
                if status["status"] != "pending":
                    break
                time.sleep(0.05)

        self.assertEqual(status["status"], "ready", status)
        return status["session_id"]

    def test_full_session_lifecycle(self):
        session_id = self._start_session_and_wait()

        pair_response = self.client.get(f"/sessions/{session_id}/next-pair")
        self.assertEqual(pair_response.status_code, 200)
        pair = pair_response.json()
        self.assertFalse(pair["done"])
        self.assertIsNotNone(pair["left"])
        self.assertIsNotNone(pair["right"])

        result_response = self.client.post(f"/sessions/{session_id}/result", json={"choice": "left"})
        self.assertEqual(result_response.status_code, 200)

        session = service._sessions[session_id]
        with patch.object(session.rating_system, "write_results_to_file") as write_mock:
            end_response = self.client.delete(f"/sessions/{session_id}")
        write_mock.assert_called_once()
        self.assertEqual(end_response.status_code, 200)

        # session is gone after ending
        missing_response = self.client.get(f"/sessions/{session_id}/next-pair")
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(missing_response.json()["detail"]["error"], "SessionNotFoundError")

    def test_result_without_pending_pair_returns_conflict(self):
        session_id = self._start_session_and_wait()

        response = self.client.post(f"/sessions/{session_id}/result", json={"choice": "left"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "NoPairAvailableError")

    def test_unknown_job_returns_404(self):
        response = self.client.get("/sessions/does-not-exist")

        self.assertEqual(response.status_code, 404)

    def test_health_check(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_tags_endpoint(self):
        with patch("tagrank.pool.load_ratings", return_value={"a": (10.0, 2.0), "b": (30.0, 1.0)}):
            response = self.client.get("/tags")

        self.assertEqual(response.status_code, 200)
        tags = [entry["tag"] for entry in response.json()]
        self.assertEqual(tags, ["b", "a"])

    def test_prediction_history_endpoint(self):
        with patch("tagrank.service.get_prediction_history", return_value=[{"date": "2024-01-01"}]):
            response = self.client.get("/history/predictions")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"date": "2024-01-01"}])


if __name__ == "__main__":
    unittest.main()
