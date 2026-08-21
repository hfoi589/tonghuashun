from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.queue import InMemoryStreams


def test_public_submission_accepts_a_six_digit_a_share_symbol() -> None:
    """A missing task route would make valid public requests impossible to queue."""
    client = TestClient(create_app())

    response = client.post("/api/v1/jobs", json={"symbol": "600938"})

    assert response.status_code == 202
    body = response.json()
    assert body["public_id"] != "600938"
    assert body["symbol"] == "600938"
    assert body["status"] == "QUEUED"
    assert body["captures"][0]["expires_at"] is None


def test_public_submission_normalizes_app_searchable_symbols_before_queueing() -> None:
    """Passing raw user spelling to the runner would make an exact app search unsafe."""
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))

    response = client.post("/api/v1/jobs", json={"symbol": "  aapl.us  "})

    assert response.status_code == 202
    public_id = response.json()["public_id"]
    assert response.json()["symbol"] == "AAPL.US"
    assert store.get(public_id).symbol == "AAPL.US"


def test_symbol_length_is_checked_after_trimming() -> None:
    """Applying the length bound to raw padding would reject a valid exact search code."""
    client = TestClient(create_app())

    response = client.post("/api/v1/jobs", json={"symbol": f"{' ' * 20}brk.b{' ' * 20}"})

    assert response.status_code == 202
    assert response.json()["symbol"] == "BRK.B"


def test_public_submission_rejects_symbols_outside_the_app_search_alphabet() -> None:
    """Accepting separators outside the approved alphabet could alter an exact app search."""
    client = TestClient(create_app())

    response = client.post("/api/v1/jobs", json={"symbol": "AAPL/US"})

    assert response.status_code == 422


def test_public_status_can_be_retrieved_with_the_opaque_task_id() -> None:
    """Discarding task state after submission would make the result URL unusable."""
    client = TestClient(create_app())
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]

    response = client.get(f"/api/v1/jobs/{public_id}")

    assert response.status_code == 200
    assert response.json()["public_id"] == public_id
    assert len(response.json()["captures"]) == 3


def test_public_submission_returns_queue_full_at_global_cap() -> None:
    """Turning a capped queue into a successful response misleads the requester."""
    client = TestClient(create_app(store=InMemoryStreams(pending_cap=1)))
    assert client.post("/api/v1/jobs", json={"symbol": "600938"}).status_code == 202

    response = client.post("/api/v1/jobs", json={"symbol": "000001"})
    assert response.status_code == 429
    assert response.json() == {"detail": "queue is full"}
