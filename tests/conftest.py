from config import key


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
