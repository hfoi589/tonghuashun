from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import MetricKind, TaskRecord, TaskStatus
from level2_service.parsed_values import (
    DirectRequestError,
    SymbolLookup,
    SymbolLookupNotFoundError,
)
from level2_service.queue import InMemoryStreams
from level2_service.symbol_cache import InMemorySymbolLookupCache


class FakeSymbolLookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> SymbolLookup:
        self.calls.append(symbol)
        if symbol == "600142":
            raise SymbolLookupNotFoundError(symbol)
        if symbol == "600999":
            raise DirectRequestError("SYMBOL_LOOKUP_FAILED", "App lookup unavailable")
        return SymbolLookup(
            symbol=symbol,
            name={"600143": "金发科技", "600938": "中国海油"}.get(symbol, "测试股票"),
            market="17" if symbol.startswith("6") else "33",
            market_label="沪A" if symbol.startswith("6") else "深A",
            securities_code=None,
        )


class FakeSymbolSearch:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def __call__(self, query: str, limit: int) -> list[SymbolLookup]:
        self.calls.append((query, limit))
        if self.fail:
            raise DirectRequestError("SYMBOL_LOOKUP_TIMEOUT", "App search timed out")
        if query == "空结果":
            return []
        return [
            SymbolLookup(
                symbol="688027",
                name="国盾量子",
                market="17",
                market_label="科创",
                securities_code=None,
            )
        ][:limit]


def app_with_symbol_lookup(*, store: InMemoryStreams | None = None):
    return create_app(store=store, symbol_lookup=FakeSymbolLookup())


def test_public_symbol_search_returns_app_candidates_without_confirming_them() -> None:
    lookup = FakeSymbolLookup()
    search = FakeSymbolSearch()
    app = create_app(symbol_lookup=lookup, symbol_search=search)

    response = TestClient(app).get(
        "/api/v1/symbols",
        params={"query": "国盾", "limit": 8},
    )

    assert response.status_code == 200
    assert response.json() == [{
        "symbol": "688027",
        "name": "国盾量子",
        "market": "17",
        "market_label": "科创",
    }]
    assert search.calls == [("国盾", 8)]
    assert lookup.calls == []


def test_public_symbol_search_returns_empty_results_and_validates_parameters() -> None:
    client = TestClient(create_app(symbol_search=FakeSymbolSearch()))

    assert client.get("/api/v1/symbols", params={"query": "空结果"}).json() == []
    assert client.get("/api/v1/symbols", params={"query": "国"}).status_code == 422
    assert client.get("/api/v1/symbols", params={"query": "国盾", "limit": 0}).status_code == 422
    assert client.get("/api/v1/symbols", params={"query": "国盾", "limit": 9}).status_code == 422
    assert client.get("/api/v1/symbols", params={"query": "  "}).status_code == 422


def test_public_symbol_search_reports_app_failure_and_does_not_seed_exact_cache() -> None:
    cache = InMemorySymbolLookupCache()
    lookup = FakeSymbolLookup()
    failing = TestClient(create_app(
        symbol_lookup=lookup,
        symbol_search=FakeSymbolSearch(fail=True),
        symbol_lookup_cache=cache,
    ))

    response = failing.get("/api/v1/symbols", params={"query": "国盾"})

    assert response.status_code == 503
    assert cache.get("688027") is None

    successful = TestClient(create_app(
        symbol_lookup=lookup,
        symbol_search=FakeSymbolSearch(),
        symbol_lookup_cache=cache,
    ))
    assert successful.get("/api/v1/symbols", params={"query": "国盾"}).status_code == 200
    assert successful.get("/api/v1/symbols/688027").status_code == 200
    assert lookup.calls == ["688027"]


def test_public_submission_accepts_a_six_digit_a_share_symbol() -> None:
    """A missing task route would make valid public requests impossible to queue."""
    client = TestClient(create_app())

    response = client.post("/api/v1/jobs", json={"symbol": "600938"})

    assert response.status_code == 202
    body = response.json()
    assert body["public_id"] != "600938"
    assert body["symbol"] == "600938"
    assert body["status"] == "QUEUED"
    assert body["include_long_capture"] is True
    assert body["long_capture"]["status"] == "PENDING"
    assert body["captures"][0]["expires_at"] is None


def test_public_submission_can_skip_the_long_capture() -> None:
    """A data-only request must remain distinguishable after it enters the queue."""
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))

    response = client.post(
        "/api/v1/jobs",
        json={"symbol": "601872", "include_long_capture": False},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["include_long_capture"] is False
    assert body["long_capture"] == {
        "status": "SKIPPED",
        "url": None,
        "expires_at": None,
    }
    assert store.get(body["public_id"]).include_long_capture is False


def test_data_only_submission_rejects_an_unknown_market_prefix_before_queueing() -> None:
    """The service must not guess a market code for an App-signed direct request."""
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))

    response = client.post(
        "/api/v1/jobs",
        json={"symbol": "AAPL.US", "include_long_capture": False},
    )

    assert response.status_code == 422
    assert store.queue_position("AAPL.US") is None


def test_public_submission_rejects_non_six_digit_symbols_before_queueing() -> None:
    """The public queue now accepts only stock codes verified by the App lookup."""
    store = InMemoryStreams()
    client = TestClient(app_with_symbol_lookup(store=store))

    response = client.post("/api/v1/jobs", json={"symbol": "  aapl.us  "})

    assert response.status_code == 422


def test_symbol_length_is_checked_after_trimming() -> None:
    """Whitespace around a valid six-digit code is harmless before lookup."""
    app = app_with_symbol_lookup()
    client = TestClient(app)

    response = client.post("/api/v1/jobs", json={"symbol": f"{' ' * 20}600143{' ' * 20}"})

    assert response.status_code == 202
    assert response.json()["symbol"] == "600143"


def test_public_submission_rejects_symbols_outside_the_app_search_alphabet() -> None:
    """Accepting separators outside the approved alphabet could alter an exact app search."""
    client = TestClient(create_app())

    response = client.post("/api/v1/jobs", json={"symbol": "AAPL/US"})

    assert response.status_code == 422


def test_public_symbol_lookup_returns_the_exact_app_name_and_market() -> None:
    """A six-digit code must be identified before the form can enable submission."""
    app = app_with_symbol_lookup()
    client = TestClient(app)

    response = client.get("/api/v1/symbols/600143")

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "600143",
        "name": "金发科技",
        "market": "17",
    }


def test_public_symbol_lookup_returns_not_found_for_an_empty_app_result() -> None:
    """The browser must keep submit disabled when the App finds no exact stock."""
    client = TestClient(app_with_symbol_lookup())

    response = client.get("/api/v1/symbols/600142")

    assert response.status_code == 404
    assert response.json() == {"detail": "symbol not found"}


def test_public_symbol_lookup_rejects_malformed_or_unsupported_codes() -> None:
    """Unsupported codes must not reach the App or the public queue."""
    app = app_with_symbol_lookup()
    client = TestClient(app)

    assert client.get("/api/v1/symbols/60014").status_code == 422
    assert client.get("/api/v1/symbols/AAPL.US").status_code == 422
    assert client.get("/api/v1/symbols/430001").status_code == 422
    assert app.state.symbol_lookup.calls == []


def test_public_symbol_lookup_reports_temporary_app_failure() -> None:
    """An offline App is different from an unknown stock and remains retryable."""
    client = TestClient(app_with_symbol_lookup())

    response = client.get("/api/v1/symbols/600999")

    assert response.status_code == 503
    assert response.json() == {"detail": "symbol lookup temporarily unavailable"}


def test_public_submission_requires_a_verified_symbol_even_when_lookup_was_bypassed() -> None:
    """Calling POST directly must not enqueue a code the App cannot resolve."""
    store = InMemoryStreams()
    app = app_with_symbol_lookup(store=store)
    client = TestClient(app)

    response = client.post("/api/v1/jobs", json={"symbol": "600142"})

    assert response.status_code == 404
    assert app.state.symbol_lookup.calls == ["600142"]


def test_public_submission_reuses_the_recent_successful_lookup() -> None:
    """Submitting after inline validation must not repeat the App call."""
    app = app_with_symbol_lookup()
    client = TestClient(app)

    assert client.get("/api/v1/symbols/600143").status_code == 200
    response = client.post("/api/v1/jobs", json={"symbol": "600143"})

    assert response.status_code == 202
    assert app.state.symbol_lookup.calls == ["600143"]


def test_public_submission_reuses_existing_symbol_task_instead_of_creating_a_new_id() -> None:
    store = InMemoryStreams()
    existing = TaskRecord(task_id="existing-task", symbol="600143", include_long_capture=False)
    store.enqueue(existing)
    store.next_queued()
    store.complete_result(existing.task_id, {MetricKind.STOCK_NAME: "金发科技"}, None)
    client = TestClient(app_with_symbol_lookup(store=store))

    response = client.post(
        "/api/v1/jobs",
        json={"symbol": "600143", "include_long_capture": True},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["public_id"] == "existing-task"
    assert body["status"] == "QUEUED"
    assert body["include_long_capture"] is True
    assert body["values"]["stock_name"] is None
    assert store.next_queued().task_id == "existing-task"


def test_public_retry_reuses_the_task_id_and_is_available_without_admin_session() -> None:
    store = InMemoryStreams()
    existing = TaskRecord(task_id="retry-task", symbol="600143", include_long_capture=False)
    store.enqueue(existing)
    store.next_queued()
    store.transition(existing.task_id, TaskStatus.FAILED, error_code="DIRECT_APP_OFFLINE")
    client = TestClient(app_with_symbol_lookup(store=store))

    response = client.post("/api/v1/jobs/retry-task/retry")

    assert response.status_code == 202
    assert response.json()["public_id"] == "retry-task"
    assert response.json()["status"] == "QUEUED"
    assert response.json()["error_code"] is None


def test_public_status_transparently_migrates_an_old_duplicate_id() -> None:
    store = InMemoryStreams()
    older = TaskRecord(task_id="older-task", symbol="600143", include_long_capture=False)
    older.created_at = older.created_at.replace(year=2025)
    older.updated_at = older.created_at
    newer = TaskRecord(task_id="newer-task", symbol="600143", include_long_capture=False)
    store.enqueue(older)
    store.enqueue(newer)
    store.deduplicate_by_symbol()
    client = TestClient(app_with_symbol_lookup(store=store))

    response = client.get("/api/v1/jobs/older-task")

    assert response.status_code == 200
    assert response.json()["public_id"] == "newer-task"

    retry = client.post("/api/v1/jobs/older-task/retry")
    events = client.get("/api/v1/jobs/older-task/events?once=true")

    assert retry.status_code == 202
    assert retry.json()["public_id"] == "newer-task"
    assert '"public_id": "newer-task"' in events.text


def test_verified_symbol_cache_is_reused_by_a_restarted_app() -> None:
    """A service restart must not make the same code query the Android App again."""
    cache = InMemorySymbolLookupCache()
    first_lookup = FakeSymbolLookup()
    first_app = create_app(symbol_lookup=first_lookup, symbol_lookup_cache=cache)

    assert TestClient(first_app).get("/api/v1/symbols/600143").status_code == 200

    def must_not_query(_symbol: str) -> SymbolLookup:
        raise AssertionError("cached symbol queried the App again")

    restarted = create_app(symbol_lookup=must_not_query, symbol_lookup_cache=cache)
    response = TestClient(restarted).get("/api/v1/symbols/600143")

    assert response.status_code == 200
    assert response.json()["name"] == "金发科技"
    assert first_lookup.calls == ["600143"]


def test_verified_symbol_cache_still_works_when_the_app_lookup_is_offline() -> None:
    """Permanent cached metadata must not depend on the Android bridge staying online."""
    cache = InMemorySymbolLookupCache()
    cache.set(SymbolLookup(symbol="600143", name="金发科技", market="17"))
    app = create_app(symbol_lookup=None, symbol_lookup_cache=cache)

    response = TestClient(app).get("/api/v1/symbols/600143")

    assert response.status_code == 200
    assert response.json()["name"] == "金发科技"


def test_cache_only_submission_rejects_an_uncached_symbol() -> None:
    """An offline lookup must not turn POST into an unchecked queue bypass."""
    cache = InMemorySymbolLookupCache()
    app = create_app(symbol_lookup=None, symbol_lookup_cache=cache)

    response = TestClient(app).post("/api/v1/jobs", json={"symbol": "600938"})

    assert response.status_code == 503


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


def test_public_status_reports_fifo_queue_position_until_a_job_is_claimed() -> None:
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))

    first = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()
    second = client.post("/api/v1/jobs", json={"symbol": "000001"}).json()

    assert first["queue_position"] == 1
    assert second["queue_position"] == 2
    store.next_queued()
    assert client.get(f"/api/v1/jobs/{first['public_id']}").json()["queue_position"] is None
    assert client.get(f"/api/v1/jobs/{second['public_id']}").json()["queue_position"] == 1
