"""Phase-aware market quotes for the pre-open and call-auction windows."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import time
from threading import RLock
from typing import Callable, Protocol

from .market_data import (
    MarketPhase,
    MarketPhaseResolver,
    MarketPhaseState,
    MarketSeriesPage,
    MarketSnapshot,
    SHANGHAI_TZ,
    fixed_market_error_code,
)
from .parsed_values import DirectRequestError
from .parsed_values import market_code_for_symbol
from .models import MetricKind


class Preopen9528Protocol(Protocol):
    def read_phase(self, session: object) -> MarketPhaseState | None: ...

    def read_quote(
        self,
        session: object,
        symbol: str,
        market: str,
        *,
        detail: bool,
        phase: MarketPhase,
    ) -> MarketSnapshot: ...


class Preopen9528Client:
    """Session-safe adapter for the verified pre-open 9528 protocol."""

    def __init__(self, session_provider: object, protocol: Preopen9528Protocol) -> None:
        self.session_provider = session_provider
        self.protocol = protocol
        self._request_lock = RLock()

    def _session(self) -> object:
        get = getattr(self.session_provider, "get", None)
        session = get("core_metrics") if callable(get) else None
        if session is None:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        return session

    def read_phase(self) -> MarketPhaseState | None:
        with self._request_lock:
            try:
                return self.protocol.read_phase(self._session())
            except DirectRequestError:
                raise
            except Exception:
                raise DirectRequestError("AUCTION_PHASE_UNAVAILABLE") from None

    def read_quote(
        self,
        symbol: str,
        *,
        detail: bool,
        phase: MarketPhase,
    ) -> MarketSnapshot:
        market = market_code_for_symbol(symbol)
        with self._request_lock:
            try:
                result = self.protocol.read_quote(
                    self._session(),
                    symbol,
                    market,
                    detail=detail,
                    phase=phase,
                )
            except DirectRequestError:
                raise
            except Exception:
                raise DirectRequestError("AUCTION_QUOTE_UNAVAILABLE") from None
        if not isinstance(result, MarketSnapshot):
            raise DirectRequestError("AUCTION_RESPONSE_INVALID")
        return result

    def close(self) -> None:
        close = getattr(self.protocol, "close", None)
        if callable(close):
            close()


class CoreDirectPreopenQuoteClient:
    """Adapt the verified 43/7001 result into a MarketSnapshot quote."""

    def __init__(
        self,
        direct_source: object,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.direct_source = direct_source
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read_phase(self) -> MarketPhaseState | None:
        # 4051 is supplied by the optional auction protocol. Until that
        # contract is verified, MarketPhaseResolver provides the safe clock
        # fallback while this client supplies only the verified quote fields.
        return None

    def read_quote(
        self,
        symbol: str,
        *,
        detail: bool,
        phase: MarketPhase,
    ) -> MarketSnapshot:
        del detail
        read_direct = getattr(self.direct_source, "read_direct", None)
        if not callable(read_direct):
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        try:
            outcome = read_direct(symbol)
        except DirectRequestError:
            raise
        except Exception:
            raise DirectRequestError("DIRECT_REQUEST_FAILED") from None
        values = getattr(outcome, "values", {})
        price = values.get(MetricKind.CURRENT_PRICE)
        change = values.get(MetricKind.CHANGE_PERCENT)
        if price in (None, "") or change in (None, ""):
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
        name = values.get(MetricKind.STOCK_NAME)
        market = market_code_for_symbol(symbol)
        precision = 3 if market in {"20", "36"} else 2
        now = self.clock().astimezone(SHANGHAI_TZ)
        return MarketSnapshot(
            symbol=symbol,
            name=str(name) if name not in (None, "") else None,
            market=market,
            sequence=0,
            source_time=now.strftime("%H:%M:%S"),
            collected_at=self.clock(),
            quote={
                "price": str(price),
                "change_percent": str(change),
                "previous_close": None,
                "open": None,
                "high": None,
                "low": None,
                "turnover_rate": None,
                "volume": None,
                "amount": None,
            },
            source="THS_DIRECT_QUOTE",
            market_phase=phase,
            market_phase_source="LOCAL_CALENDAR",
            price_precision=precision,
            capabilities={
                "auction": {
                    "available": True,
                    "reason": "VERIFIED_43_7001_FALLBACK",
                },
            },
            source_errors={
                "market_phase": None,
                "auction_quote": None,
                "core_metrics": None,
                "main_fund_flow": None,
            },
        )

    def close(self) -> None:
        close = getattr(self.direct_source, "close", None)
        if callable(close):
            close()


class PreopenQuoteClient(Protocol):
    def read_phase(self) -> MarketPhaseState | None: ...

    def read_quote(
        self,
        symbol: str,
        *,
        detail: bool,
        phase: MarketPhase,
    ) -> MarketSnapshot: ...


class PreopenQuoteCache(Protocol):
    def get(self, symbol: str, trade_date: date) -> MarketSnapshot | None: ...

    def set(self, snapshot: MarketSnapshot, trade_date: date) -> None: ...


class RedisPreopenQuoteCache:
    """Persist only sanitized same-day quote fields, never session material."""

    def __init__(self, redis: object, *, prefix: str = "ths:market:auction", ttl_seconds: int = 64800) -> None:
        self.redis = redis
        self.prefix = prefix.strip(":")
        self.ttl_seconds = max(60, int(ttl_seconds))

    def _key(self, symbol: str, trade_date: date) -> str:
        return f"{self.prefix}:{trade_date.isoformat()}:{symbol}"

    def set(self, snapshot: MarketSnapshot, trade_date: date) -> None:
        setex = getattr(self.redis, "setex", None)
        if not callable(setex):
            return
        payload = {
            "symbol": snapshot.symbol,
            "name": snapshot.name,
            "market": snapshot.market,
            "source_time": snapshot.source_time,
            "collected_at": snapshot.collected_at.isoformat(),
            "quote": dict(snapshot.quote),
            "source": snapshot.source,
            "market_phase": snapshot.market_phase.value,
            "market_phase_source": snapshot.market_phase_source,
            "price_precision": snapshot.price_precision,
            "capabilities": dict(snapshot.capabilities),
            "source_errors": dict(snapshot.source_errors),
        }
        setex(self._key(snapshot.symbol, trade_date), self.ttl_seconds, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def get(self, symbol: str, trade_date: date) -> MarketSnapshot | None:
        getter = getattr(self.redis, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter(self._key(symbol, trade_date))
            if raw is None:
                return None
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
            collected_at = datetime.fromisoformat(str(payload["collected_at"]))
            phase = MarketPhase(str(payload.get("market_phase", MarketPhase.CLOSED.value)))
            return MarketSnapshot(
                symbol=str(payload["symbol"]),
                name=payload.get("name"),
                market=str(payload["market"]),
                sequence=0,
                source_time=payload.get("source_time"),
                collected_at=collected_at,
                quote=dict(payload.get("quote", {})),
                source=payload.get("source"),
                market_phase=phase,
                market_phase_source=payload.get("market_phase_source"),
                price_precision=int(payload.get("price_precision", 2)),
                capabilities=dict(payload.get("capabilities", {})),
                source_errors=dict(payload.get("source_errors", {})),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None


class PreopenMarketDataSource:
    """Switch public and verified App-internal quotes by market phase.

    The source deliberately does not call the public provider during an active
    pre-open phase. A direct failure may only return a current-day cached value.
    """

    _PREOPEN_PHASES = frozenset(
        {
            MarketPhase.PREOPEN_QUOTE,
            MarketPhase.CALL_AUCTION,
            MarketPhase.AUCTION_LOCKED,
        }
    )

    def __init__(
        self,
        base_source: object,
        preopen_client: PreopenQuoteClient | None,
        *,
        phase_resolver: MarketPhaseResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        enforce_preopen: bool = False,
        cache: PreopenQuoteCache | None = None,
    ) -> None:
        self.base_source = base_source
        self.preopen_client = preopen_client
        self.phase_resolver = phase_resolver or MarketPhaseResolver()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.enforce_preopen = enforce_preopen
        self.cache = cache
        self._cache: dict[str, tuple[date, MarketSnapshot]] = {}

    def _phase(self) -> tuple[MarketPhaseState, str | None]:
        error_code: str | None = None
        if self.preopen_client is not None:
            try:
                state = self.preopen_client.read_phase()
            except DirectRequestError as error:
                state = None
                error_code = fixed_market_error_code(error, "AUCTION_PHASE_UNAVAILABLE")
            except Exception:
                state = None
                error_code = "AUCTION_PHASE_UNAVAILABLE"
            else:
                error_code = None
            if state is not None:
                return state, error_code
        return self.phase_resolver.resolve(self.clock()), error_code

    def _cache_date(self) -> date:
        return self.clock().astimezone(SHANGHAI_TZ).date()

    def _cached(self, symbol: str) -> MarketSnapshot | None:
        entry = self._cache.get(symbol)
        today = self._cache_date()
        if entry is not None and entry[0] == today:
            return entry[1]
        if self.cache is None:
            return None
        try:
            restored = self.cache.get(symbol, today)
        except Exception:
            restored = None
        if restored is not None:
            self._cache[symbol] = (today, restored)
        return restored

    def _remember(self, snapshot: MarketSnapshot) -> None:
        # Public continuous quotes must not replace an auction value. The
        # cache is used only as a same-day auction recovery source.
        if snapshot.source not in {"THS_AUCTION", "THS_DIRECT_QUOTE"}:
            return
        today = self._cache_date()
        self._cache[snapshot.symbol] = (today, snapshot)
        if self.cache is not None:
            try:
                self.cache.set(snapshot, today)
            except Exception:
                pass

    def seed(self, snapshot: MarketSnapshot) -> None:
        self._remember(snapshot)

    @staticmethod
    def _with_phase(
        snapshot: MarketSnapshot,
        state: MarketPhaseState,
        *,
        phase_error: str | None = None,
        auction_error: str | None = None,
    ) -> MarketSnapshot:
        errors = dict(snapshot.source_errors)
        errors.setdefault("market_phase", None)
        errors.setdefault("auction_quote", None)
        errors["market_phase"] = phase_error
        errors["auction_quote"] = auction_error
        capabilities = dict(snapshot.capabilities)
        auction = dict(capabilities.get("auction", {}))
        if state.phase in PreopenMarketDataSource._PREOPEN_PHASES:
            auction["available"] = auction_error is None
            auction["reason"] = auction_error
        else:
            auction["available"] = False
            auction["reason"] = "MARKET_PHASE_NOT_AUCTION"
        capabilities["auction"] = auction
        return replace(
            snapshot,
            market_phase=state.phase,
            market_phase_source=state.source,
            capabilities=capabilities,
            source_errors=errors,
        )

    def _read_base(self, symbol: str, *, detail: bool, state: MarketPhaseState, phase_error: str | None) -> MarketSnapshot:
        read_snapshot = getattr(self.base_source, "read_market_snapshot", None)
        if not callable(read_snapshot):
            raise DirectRequestError("MARKET_QUOTE_UNAVAILABLE")
        snapshot = read_snapshot(symbol, detail=detail)
        current = self._with_phase(snapshot, state, phase_error=phase_error)
        self._remember(current)
        return current

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        state, phase_error = self._phase()
        if state.phase not in self._PREOPEN_PHASES:
            return self._read_base(symbol, detail=detail, state=state, phase_error=phase_error)
        if self.preopen_client is None:
            if not self.enforce_preopen:
                return self._read_base(symbol, detail=detail, state=state, phase_error=phase_error)
            cached = self._cached(symbol)
            if cached is not None:
                return self._with_phase(
                    cached,
                    state,
                    phase_error=phase_error,
                    auction_error="DIRECT_SESSION_UNAVAILABLE",
                )
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        try:
            read_quote = getattr(self.preopen_client, "read_quote", None)
            if not callable(read_quote):
                raise DirectRequestError("AUCTION_QUOTE_UNAVAILABLE")
            snapshot = read_quote(symbol, detail=detail, phase=state.phase)
            current = self._with_phase(snapshot, state, phase_error=phase_error)
            self._remember(current)
            return current
        except DirectRequestError as error:
            error_code = fixed_market_error_code(error, "AUCTION_QUOTE_UNAVAILABLE")
        except Exception:
            error_code = "AUCTION_QUOTE_UNAVAILABLE"
        cached = self._cached(symbol)
        if cached is None:
            raise DirectRequestError(error_code)
        return self._with_phase(
            cached,
            state,
            phase_error=phase_error,
            auction_error=error_code,
        )

    def read_market_snapshots(
        self,
        symbols: set[str],
        *,
        detail_symbols: set[str],
    ) -> dict[str, MarketSnapshot]:
        if not symbols:
            return {}
        state, phase_error = self._phase()
        batch_reader = getattr(self.preopen_client, "read_watchlist_quotes", None)
        if state.phase in self._PREOPEN_PHASES and callable(batch_reader):
            try:
                raw = batch_reader(set(symbols), phase=state.phase)
                if not isinstance(raw, dict):
                    raise DirectRequestError("AUCTION_RESPONSE_INVALID")
                result: dict[str, MarketSnapshot] = {}
                for symbol, snapshot in raw.items():
                    if symbol not in symbols or not isinstance(snapshot, MarketSnapshot):
                        continue
                    current = self._with_phase(snapshot, state, phase_error=phase_error)
                    self._remember(current)
                    result[symbol] = current
                for symbol in detail_symbols & set(result):
                    result[symbol] = self.read_market_snapshot(symbol, detail=True)
                return result
            except DirectRequestError:
                raise
            except Exception:
                raise DirectRequestError("AUCTION_QUOTE_UNAVAILABLE") from None
        return {
            symbol: self.read_market_snapshot(
                symbol,
                detail=symbol in detail_symbols,
            )
            for symbol in symbols
        }

    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        read_series = getattr(self.base_source, "read_market_series", None)
        if not callable(read_series):
            raise DirectRequestError("MARKET_SERIES_UNAVAILABLE")
        return read_series(symbol, period, cursor, limit)

    def __getattr__(self, name: str) -> object:
        return getattr(self.base_source, name)
