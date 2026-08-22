from level2_service.parsed_values import SymbolLookup
from level2_service.symbol_cache import RedisSymbolLookupCache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str):
        self.set_calls.append((key, value))
        self.values[key] = value


def test_redis_symbol_cache_survives_service_reconstruction_without_expiry() -> None:
    """A verified stock must remain reusable after the API container restarts."""
    redis = FakeRedis()
    first = RedisSymbolLookupCache(redis)
    value = SymbolLookup(
        symbol="600143",
        name="金发科技",
        market="17",
        market_label="沪A",
        securities_code=None,
    )

    first.set(value)
    restored = RedisSymbolLookupCache(redis).get("600143")

    assert restored == value
    assert len(redis.set_calls) == 1


def test_redis_symbol_cache_ignores_corrupt_or_mismatched_entries() -> None:
    """Broken cache data must fall back to a fresh App lookup, not validate the wrong code."""
    redis = FakeRedis()
    redis.values["ths:symbols:600143"] = "not-json"
    redis.values["ths:symbols:600938"] = (
        '{"symbol":"600143","name":"金发科技","market":"17",'
        '"market_label":"沪A","securities_code":null}'
    )
    cache = RedisSymbolLookupCache(redis)

    assert cache.get("600143") is None
    assert cache.get("600938") is None
