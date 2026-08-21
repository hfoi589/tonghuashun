from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.queue import InMemoryStreams


def test_public_submission_accepts_a_six_digit_a_share_symbol() -> None:
    """A missing task route would make valid public requests impossible to queue."""
    client = TestClient(create_app())

    response = client.post("/api/tasks", json={"symbol": "600938"})

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] != "600938"
    assert body["symbol"] == "600938"
    assert body["status"] == "QUEUED"


def test_public_submission_rejects_non_a_share_symbols() -> None:
    """Relaxing symbol validation would send ambiguous input to the Android runner."""
    client = TestClient(create_app())

    response = client.post("/api/tasks", json={"symbol": "ABC123"})

    assert response.status_code == 422


def test_public_status_can_be_retrieved_with_the_opaque_task_id() -> None:
    """Discarding task state after submission would make the result URL unusable."""
    client = TestClient(create_app())
    task_id = client.post("/api/tasks", json={"symbol": "600938"}).json()["task_id"]

    response = client.get(f"/api/tasks/{task_id}")

    assert response.status_code == 200
    assert response.json()["task_id"] == task_id
    assert len(response.json()["captures"]) == 3


def test_public_submission_returns_queue_full_at_global_cap() -> None:
    """Turning a capped queue into a successful response misleads the requester."""
    client = TestClient(create_app(store=InMemoryStreams(pending_cap=1)))
    assert client.post("/api/tasks", json={"symbol": "600938"}).status_code == 202

    response = client.post("/api/tasks", json={"symbol": "000001"})
    assert response.status_code == 429
    assert response.json() == {"detail": "queue is full"}
