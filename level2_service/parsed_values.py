"""Read current quote and indicator values already parsed by the THS app."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import RLock
from typing import Any, Callable, Protocol

from .models import MetricKind


APP_PACKAGE = "com.hexin.plat.android"
SHANGHAI_PREFIXES = ("600", "601", "603", "605", "688", "689")
SHENZHEN_PREFIXES = ("000", "001", "002", "003", "300", "301")
BEIJING_PREFIXES = ("920",)
SHANGHAI_FUND_PREFIXES = (
    "501",
    "502",
    "506",
    "508",
    "510",
    "511",
    "512",
    "513",
    "515",
    "516",
    "517",
    "518",
    "519",
    "520",
    "526",
    "530",
    "551",
    "560",
    "561",
    "562",
    "563",
    "588",
    "589",
)
SHENZHEN_FUND_PREFIXES = (
    "158",
    "159",
    "160",
    "161",
    "162",
    "163",
    "164",
    "165",
    "166",
    "167",
    "168",
    "169",
    "180",
)


class UnsupportedMarketError(ValueError):
    """The symbol cannot be mapped to a market code confirmed in this App build."""


class DirectRequestError(RuntimeError):
    """The App-internal quote request failed without falling back to UI automation."""

    def __init__(self, error_code: str, message: str | None = None) -> None:
        self.error_code = error_code
        super().__init__(message or error_code)


class SymbolLookupNotFoundError(LookupError):
    """The App search returned no unique exact result for the requested code."""


class SymbolLookupAmbiguousError(LookupError):
    """The App search returned more than one exact result for the requested code."""


@dataclass(frozen=True)
class SymbolLookup:
    symbol: str
    name: str
    market: str
    market_label: str | None = None
    securities_code: str | None = None


def market_code_for_symbol(symbol: str) -> str:
    normalized = str(symbol).strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise UnsupportedMarketError("direct requests require a supported six-digit symbol")
    if normalized.startswith(SHANGHAI_PREFIXES):
        return "17"
    if normalized.startswith(SHENZHEN_PREFIXES):
        return "33"
    if normalized.startswith(BEIJING_PREFIXES):
        return "151"
    if normalized.startswith(SHANGHAI_FUND_PREFIXES):
        return "20"
    if normalized.startswith(SHENZHEN_FUND_PREFIXES):
        return "36"
    raise UnsupportedMarketError(f"unsupported market prefix: {normalized[:3]}")


class ParsedValueSource(Protocol):
    def read(self, symbol: str) -> dict[MetricKind, str | None]: ...

    def read_direct(self, symbol: str) -> dict[MetricKind, str | None]: ...

    def lookup_symbol(self, symbol: str) -> SymbolLookup: ...


def empty_metric_values() -> dict[MetricKind, str | None]:
    return {kind: None for kind in MetricKind}


class FridaParsedValueSource:
    """Attach for each task and read only objects whose stock code matches."""

    def __init__(
        self,
        endpoint: str,
        *,
        package: str = APP_PACKAGE,
        timeout_seconds: float = 8,
        runtime_reader: Callable[[str, str, float], dict[str, Any]] | None = None,
        direct_reader: Callable[[str, str, float, str, str], dict[str, Any]] | None = None,
        lookup_reader: Callable[[str, str, float, str], dict[str, Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.package = package
        self.timeout_seconds = timeout_seconds
        self._runtime_reader = runtime_reader or self._read_runtime
        self._direct_reader = direct_reader or self._read_direct_runtime
        self._lookup_reader = lookup_reader or self._lookup_runtime
        self._frida_lock = RLock()

    def read(self, symbol: str) -> dict[MetricKind, str | None]:
        try:
            with self._frida_lock:
                payload = self._runtime_reader(self.endpoint, self.package, self.timeout_seconds)
            return self._parse_payload(payload, symbol)
        except Exception:
            return empty_metric_values()

    def read_direct(self, symbol: str) -> dict[MetricKind, str | None]:
        market = market_code_for_symbol(symbol)
        try:
            with self._frida_lock:
                payload = self._direct_reader(
                    self.endpoint,
                    self.package,
                    self.timeout_seconds,
                    symbol,
                    market,
                )
        except (UnsupportedMarketError, DirectRequestError):
            raise
        except Exception as error:
            raise DirectRequestError("DIRECT_REQUEST_FAILED", str(error)) from error
        error_code = _text(payload.get("error_code"))
        if error_code is not None:
            raise DirectRequestError(error_code, _text(payload.get("error_message")))
        return self._parse_payload(payload, symbol)

    def lookup_symbol(self, symbol: str) -> SymbolLookup:
        normalized = str(symbol).strip()
        expected_market = market_code_for_symbol(normalized)
        try:
            with self._frida_lock:
                payload = self._lookup_reader(
                    self.endpoint,
                    self.package,
                    self.timeout_seconds,
                    normalized,
                )
        except (UnsupportedMarketError, SymbolLookupNotFoundError, SymbolLookupAmbiguousError):
            raise
        except DirectRequestError:
            raise
        except Exception as error:
            raise DirectRequestError("SYMBOL_LOOKUP_FAILED", str(error)) from error

        error_code = _text(payload.get("error_code"))
        if error_code is not None:
            raise DirectRequestError(error_code, _text(payload.get("error_message")))

        matches: list[SymbolLookup] = []
        results = payload.get("results", ())
        if not isinstance(results, (list, tuple)):
            raise DirectRequestError("SYMBOL_LOOKUP_INVALID", "App search result is not a list")
        for item in results:
            if not isinstance(item, dict):
                continue
            stock_code = _text(item.get("stock_code"))
            market = _text(item.get("market_id"))
            name = _text(item.get("stock_name"))
            if stock_code != normalized or market != expected_market or name is None:
                continue
            matches.append(
                SymbolLookup(
                    symbol=normalized,
                    name=name,
                    market=market,
                    market_label=_text(item.get("market_label")),
                    securities_code=_text(item.get("securities_code")),
                )
            )

        if not matches:
            raise SymbolLookupNotFoundError(normalized)
        if len(matches) != 1:
            raise SymbolLookupAmbiguousError(normalized)
        return matches[0]

    @staticmethod
    def _parse_payload(payload: dict[str, Any], symbol: str) -> dict[MetricKind, str | None]:
        result = empty_metric_values()
        quote_candidates = [
            quote
            for quote in payload.get("quotes", ())
            if _symbols_match(quote.get("symbol"), symbol)
        ]
        quote = max(quote_candidates, key=_quote_score, default=None)
        if quote is not None:
            price = _decimal(quote.get("price"))
            previous_close = _decimal(quote.get("previous_close"))
            change_percent = _decimal(quote.get("change_percent"))
            if change_percent is None and price is not None and previous_close not in (None, Decimal(0)):
                change_percent = (price / previous_close - Decimal(1)) * Decimal(100)
            name = _text(quote.get("name"))
            result[MetricKind.STOCK_NAME] = name
            result[MetricKind.CURRENT_PRICE] = _format_number(price, 2)
            result[MetricKind.CHANGE_PERCENT] = _format_number(change_percent, 2, suffix="%")
            result[MetricKind.TURNOVER_RATE] = _format_number(
                _decimal(quote.get("turnover_rate")), 2, suffix="%"
            )

        latest_by_techid: dict[int, Decimal] = {}
        for indicator in payload.get("indicators", ()):
            if not _symbols_match(indicator.get("symbol"), symbol):
                continue
            try:
                techid = int(indicator.get("techid"))
            except (TypeError, ValueError):
                continue
            values = indicator.get("values")
            if not isinstance(values, (list, tuple)) or not values:
                continue
            latest = _decimal(values[-1])
            if latest is not None:
                latest_by_techid[techid] = latest

        result[MetricKind.LARGE_ORDER_NET] = _format_number(latest_by_techid.get(7031), 2)
        amount = latest_by_techid.get(7032)
        result[MetricKind.LARGE_ORDER_AMOUNT] = _format_number(
            amount / Decimal(10000) if amount is not None else None,
            1,
            suffix="万",
        )
        result[MetricKind.RETAIL_COUNT] = _format_number(latest_by_techid.get(7034), 2)
        result[MetricKind.MACDFS] = _format_number(latest_by_techid.get(7051), 3, show_plus=True)
        return result

    @staticmethod
    def _read_runtime(endpoint: str, package: str, _timeout_seconds: float) -> dict[str, Any]:
        import frida  # type: ignore[import-not-found]

        device = frida.get_device_manager().add_remote_device(endpoint)
        application = next(
            (item for item in device.enumerate_applications() if item.identifier == package and item.pid),
            None,
        )
        if application is None:
            raise RuntimeError("THS process is not running")
        session = device.attach(application.pid)
        script = session.create_script(_FRIDA_SCRIPT)
        try:
            script.load()
            return script.exports_sync.snapshot()
        finally:
            try:
                script.unload()
            finally:
                session.detach()

    @staticmethod
    def _read_direct_runtime(
        endpoint: str,
        package: str,
        timeout_seconds: float,
        symbol: str,
        market: str,
    ) -> dict[str, Any]:
        import frida  # type: ignore[import-not-found]

        device = frida.get_device_manager().add_remote_device(endpoint)
        application = next(
            (item for item in device.enumerate_applications() if item.identifier == package and item.pid),
            None,
        )
        if application is None:
            raise DirectRequestError("DIRECT_APP_OFFLINE", "THS process is not running")
        session = device.attach(application.pid)
        script = session.create_script(_FRIDA_DIRECT_SCRIPT)
        try:
            script.load()
            return script.exports_sync.request(
                symbol,
                market,
                max(1000, round(timeout_seconds * 1000)),
            )
        finally:
            try:
                script.unload()
            finally:
                session.detach()

    @staticmethod
    def _lookup_runtime(
        endpoint: str,
        package: str,
        timeout_seconds: float,
        symbol: str,
    ) -> dict[str, Any]:
        import frida  # type: ignore[import-not-found]

        device = frida.get_device_manager().add_remote_device(endpoint)
        application = next(
            (item for item in device.enumerate_applications() if item.identifier == package and item.pid),
            None,
        )
        if application is None:
            raise DirectRequestError("SYMBOL_LOOKUP_APP_OFFLINE", "THS process is not running")
        session = device.attach(application.pid)
        script = session.create_script(_FRIDA_SYMBOL_LOOKUP_SCRIPT)
        try:
            script.load()
            return script.exports_sync.lookup(
                symbol,
                max(1000, round(timeout_seconds * 1000)),
            )
        finally:
            try:
                script.unload()
            finally:
                session.detach()


def _quote_score(quote: dict[str, Any]) -> int:
    return sum(
        _text(quote.get(field)) is not None
        for field in ("name", "price", "change_percent", "turnover_rate", "previous_close")
    )


def _symbols_match(candidate: object, requested: object) -> bool:
    candidate_text = _text(candidate)
    requested_text = _text(requested)
    if candidate_text is None or requested_text is None:
        return False
    candidate_text = candidate_text.upper()
    requested_text = requested_text.upper()
    if candidate_text == requested_text:
        return True
    candidate_digits = "".join(character for character in candidate_text if character.isdigit())
    requested_digits = "".join(character for character in requested_text if character.isdigit())
    return len(candidate_digits) == 6 and candidate_digits == requested_digits


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "undefined"}:
        return None
    return normalized


def _decimal(value: object) -> Decimal | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        number = Decimal(normalized)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _format_number(
    value: Decimal | None,
    places: int,
    *,
    suffix: str = "",
    show_plus: bool = False,
) -> str | None:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    prefix = "+" if show_plus and rounded > 0 else ""
    return f"{prefix}{rounded:.{places}f}{suffix}"


_FRIDA_SCRIPT = r"""
rpc.exports = {
  snapshot: function () {
    return new Promise(function (resolve) {
      Java.perform(function () {
        var payload = { quotes: [], indicators: [] };
        var pending = 2;
        var Lxg = Java.use('lxg');
        var Entry = Java.use('qxg$e');

        function finish() {
          pending -= 1;
          if (pending === 0) resolve(payload);
        }

        function scalar(value) {
          if (value === null || value === undefined) return null;
          var text = String(value);
          return text === 'null' || text === 'undefined' ? null : text;
        }

        function ext(source, dataId) {
          try { return scalar(source.getExtData(dataId)); } catch (_) { return null; }
        }

        function seriesLast(source, dataId) {
          try {
            var values = source.getData(dataId);
            if (values === null || values.length === 0) return null;
            return scalar(values[values.length - 1]);
          } catch (_) { return null; }
        }

        function quote(source) {
          return {
            symbol: ext(source, 4),
            name: ext(source, 55),
            price: ext(source, 10) || seriesLast(source, 10),
            previous_close: ext(source, 6),
            change_percent: ext(source, 34315) || seriesLast(source, 34315),
            turnover_rate: ext(source, 34312) || seriesLast(source, 34312)
          };
        }

        Java.choose('com.hexin.middleware.data.mobile.StuffTableStruct', {
          onMatch: function (table) {
            try { payload.quotes.push(quote(table)); } catch (_) {}
          },
          onComplete: finish
        });

        Java.choose('xzg', {
          onMatch: function (indicator) {
            try {
              var techid = indicator._k.value;
              var dataId = ({7031: 33007, 7032: 33015, 7034: 216, 7051: 36883})[techid];
              var parser = indicator._h.value;
              if (parser === null) return;
              var parsed = Java.cast(parser, Lxg);
              var source = parsed._c.value;
              if (source === null) return;
              payload.quotes.push(quote(source));
              if (dataId === undefined) return;
              var resultTable = parsed._h.value;
              if (resultTable === null) return;
              var result = resultTable.e(dataId);
              if (result === null) return;
              var rawValues = Entry.d.call(result);
              var values = [];
              for (var index = 0; index < rawValues.length; index += 1) {
                values.push(rawValues[index]);
              }
              payload.indicators.push({
                symbol: ext(source, 4),
                techid: techid,
                values: values
              });
            } catch (_) {}
          },
          onComplete: finish
        });
      });
    });
  }
};
"""


_FRIDA_SYMBOL_LOOKUP_SCRIPT = r"""
rpc.exports = {
  lookup: function (requestedSymbol, timeoutMilliseconds) {
    return new Promise(function (resolve) {
      var settled = false;
      var timeout = setTimeout(function () {
        complete({
          error_code: 'SYMBOL_LOOKUP_TIMEOUT',
          error_message: 'App stock search timed out'
        });
      }, Math.max(1000, Number(timeoutMilliseconds)));

      function complete(result) {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        resolve(result);
      }

      function scalar(value) {
        if (value === null || value === undefined) return null;
        var text = String(value).trim();
        return text.length === 0 || text === 'null' || text === 'undefined' ? null : text;
      }

      Java.perform(function () {
        var viewModel = null;
        Java.choose('com.hexin.android.biz_sub_search.search.associate.AssociateViewModel', {
          onMatch: function (candidate) {
            if (viewModel === null) viewModel = Java.retain(candidate);
          },
          onComplete: function () {
            if (viewModel === null) {
              complete({
                error_code: 'SYMBOL_LOOKUP_UNAVAILABLE',
                error_message: 'App search is not active'
              });
              return;
            }
            Java.scheduleOnMainThread(function () {
              try {
                var StockAssociateData = Java.use(
                  'com.hexin.android.biz_sub_search.search.StockAssociateData'
                );
                var SearchStockDataCell = Java.use(
                  'com.hexin.android.biz_sub_search.search.SearchStockDataCell'
                );
                var wrapped = viewModel.queryStockDataList(String(requestedSymbol));
                var data = Java.cast(wrapped.getData(), StockAssociateData);
                var rows = data.getData();
                var results = [];
                for (var index = 0; index < rows.size(); index += 1) {
                  var row = Java.cast(rows.get(index), SearchStockDataCell);
                  results.push({
                    stock_code: scalar(row.getStockCode()),
                    stock_name: scalar(row.getStockName()),
                    market_id: scalar(row.getStockMarketId()),
                    market_label: scalar(row.getMarketLabel()),
                    securities_code: scalar(row.getSecuritiesCode())
                  });
                }
                complete({ results: results });
              } catch (error) {
                complete({
                  error_code: 'SYMBOL_LOOKUP_FAILED',
                  error_message: String(error.message || error)
                });
              }
            });
          }
        });
      });
    });
  }
};
"""


_FRIDA_DIRECT_SCRIPT = r"""
rpc.exports = {
  request: function (requestedSymbol, requestedMarket, timeoutMilliseconds) {
    return new Promise(function (resolve) {
      Java.perform(function () {
        var Group = Java.use('qwg$i');
        var Ayg = Java.use('ayg');
        var Stock = Java.use('com.hexin.android.biz_frame.eqframe.event.struct.EQBasicStockInfo');
        var CopyList = Java.use('java.util.concurrent.CopyOnWriteArrayList');
        var HashMap = Java.use('java.util.HashMap');
        var Integer = Java.use('java.lang.Integer');
        var CurveTech = Java.use('com.hexin.android.finance_chart.domain.CurveTech');
        var CurveXmlConfig = Java.use('com.hexin.android.finance_chart.domain.CurveXmlConfig');
        var TechDataManager = Java.use('com.hexin.android.finance_chart.business.indicator.business.multi.TechDataManager');
        var IndicatorFactory = Java.use('ccg');
        var CurveParser = Java.use('lxg');
        var ResultEntry = Java.use('qxg$e');
        var CurveRegistry = Java.use('rwg');
        var symbol = String(requestedSymbol);
        var market = String(requestedMarket);
        var deadline = Date.now() + Math.max(1000, Number(timeoutMilliseconds));
        var settled = false;
        var manager = null;
        var mainGroup = null;
        var payload = { quotes: [], indicators: [], missing_techids: [] };

        function complete(result) {
          if (settled) return;
          settled = true;
          resolve(result);
        }

        function fail(code, error) {
          var message = error === null || error === undefined ? code : String(error.message || error);
          complete({ error_code: code, error_message: message });
        }

        function scalar(value) {
          if (value === null || value === undefined) return null;
          var text = String(value);
          return text === 'null' || text === 'undefined' || text.length === 0 ? null : text;
        }

        function ext(source, dataId) {
          try { return scalar(source.getExtData(dataId)); } catch (_) { return null; }
        }

        function valuesFromCurve(source, dataId) {
          try {
            if (source === null || source === undefined) return null;
            var raw = source.getData(dataId);
            if (raw === null || raw.length === 0) return null;
            var values = [];
            for (var index = 0; index < raw.length; index += 1) values.push(raw[index]);
            return values;
          } catch (_) { return null; }
        }

        function valuesFromTable(table, dataId) {
          try {
            if (table === null || table === undefined) return null;
            var entry = table.e(dataId);
            if (entry === null) return null;
            var raw = ResultEntry.d.call(entry);
            if (raw === null || raw.length === 0) return null;
            var values = [];
            for (var index = 0; index < raw.length; index += 1) values.push(raw[index]);
            return values;
          } catch (_) { return null; }
        }

        function parameters(stockSymbol, marketCode, includeInterval) {
          var map = HashMap.$new();
          map.put('stockname', stockSymbol);
          map.put('stockcode', stockSymbol);
          map.put('marketid', marketCode);
          map.put('period', '20');
          if (includeInterval) map.put('pinterval', '1');
          return map;
        }

        function removeGroup(group) {
          try {
            if (manager !== null && group !== null) manager.P0().remove(group);
          } catch (_) {}
        }

        function curveConfig() {
          var registry = CurveRegistry.i();
          var configMap = registry._k.value.c();
          var value = configMap.get(Integer.valueOf(2));
          if (value === null) throw new Error('curve configuration 2 is unavailable');
          return Java.cast(value, CurveXmlConfig);
        }

        function waitForMain(group) {
          return new Promise(function (accept, reject) {
            var timer = setInterval(function () {
              Java.perform(function () {
                try {
                  var parser = group.c() === null ? null : group.c()._h.value;
                  if (parser !== null) {
                    var parsed = Java.cast(parser, CurveParser);
                    var base = parsed._c.value;
                    if (base !== null && ext(base, 4) === symbol) {
                      clearInterval(timer);
                      accept(base);
                      return;
                    }
                  }
                  if (Date.now() >= deadline) {
                    clearInterval(timer);
                    reject({ code: 'DIRECT_REQUEST_TIMEOUT', message: 'main quote response timed out' });
                  }
                } catch (error) {
                  clearInterval(timer);
                  reject({ code: 'DIRECT_RESPONSE_INVALID', message: String(error) });
                }
              });
            }, 50);
          });
        }

        function requestMain() {
          var stock = Stock.$new(symbol, symbol, market);
          var request = Ayg.$new(43, 2, 7001, 7001, true);
          request.z(parameters(symbol, market, true));
          var group = Group.$new(manager, 1, symbol, 20, request, stock, 7001);
          var config = curveConfig();
          var tech = config.getUnit(0).get(Integer.valueOf(7001));
          if (tech === null) throw new Error('main curve definition is unavailable');
          group.y(manager.f0(43, 2, group, Java.cast(tech, CurveTech), 20));
          var requests = CopyList.$new();
          requests.add(group);
          manager.V(requests);
          manager.i2(null, requests);
          mainGroup = group;
          return waitForMain(group);
        }

        function quote(base) {
          var priceSeries = valuesFromCurve(base, 10);
          var changeSeries = valuesFromCurve(base, 34315);
          var turnoverSeries = valuesFromCurve(base, 34312);
          return {
            symbol: ext(base, 4),
            name: ext(base, 55),
            price: ext(base, 10) || (priceSeries === null ? null : scalar(priceSeries[priceSeries.length - 1])),
            previous_close: ext(base, 6),
            change_percent: ext(base, 34315) || (changeSeries === null ? null : scalar(changeSeries[changeSeries.length - 1])),
            turnover_rate: ext(base, 34312) || (turnoverSeries === null ? null : scalar(turnoverSeries[turnoverSeries.length - 1]))
          };
        }

        function requestIndicator(base, techId, dataId, requireComputed) {
          return new Promise(function (accept) {
            var group = null;
            var processor = null;
            try {
              var stock = Stock.$new(symbol, symbol, market);
              var request = Ayg.$new(44, 2, techId, 0, false);
              request.z(parameters(symbol, market, false));
              request.C(2);
              var metadata = TechDataManager._a.value.q(1, techId);
              if (metadata === null) {
                accept(null);
                return;
              }
              request.B(metadata);
              processor = IndicatorFactory.b(2, metadata);
              if (processor !== null) request.v(processor);
              var config = curveConfig();
              var tech = config.getUnit(2).get(Integer.valueOf(techId));
              if (tech === null) {
                if (processor !== null) try { processor.clear(); } catch (_) {}
                accept(null);
                return;
              }
              group = Group.$new(manager, 1, symbol, 20, request, stock, techId);
              group.y(manager.f0(44, 2, group, Java.cast(tech, CurveTech), 20));
              var requests = CopyList.$new();
              requests.add(group);
              manager.V(requests);
              manager.i2(base, requests);
            } catch (_) {
              removeGroup(group);
              if (processor !== null) try { processor.clear(); } catch (_) {}
              accept(null);
              return;
            }

            var indicatorDeadline = Math.min(deadline, Date.now() + 1500);
            var timer = setInterval(function () {
              Java.perform(function () {
                try {
                  var parser = group.c() === null ? null : group.c()._h.value;
                  if (parser !== null) {
                    var parsed = Java.cast(parser, CurveParser);
                    var rawValues = valuesFromCurve(parsed._d.value, dataId);
                    var computedValues = valuesFromTable(parsed._h.value, dataId);
                    var selected = requireComputed ? computedValues : (computedValues || rawValues);
                    if (selected !== null && selected.length > 0) {
                      clearInterval(timer);
                      removeGroup(group);
                      if (processor !== null) try { processor.clear(); } catch (_) {}
                      accept({ symbol: symbol, techid: techId, values: selected });
                      return;
                    }
                  }
                  if (Date.now() >= indicatorDeadline) {
                    clearInterval(timer);
                    removeGroup(group);
                    if (processor !== null) try { processor.clear(); } catch (_) {}
                    accept(null);
                  }
                } catch (_) {
                  clearInterval(timer);
                  removeGroup(group);
                  if (processor !== null) try { processor.clear(); } catch (_) {}
                  accept(null);
                }
              });
            }, 50);
          });
        }

        Java.choose('qwg', {
          onMatch: function (candidate) {
            try {
              if (manager === null && candidate._I.value.isAlive() && String(candidate._H.value) === '1229') {
                manager = candidate;
              }
            } catch (_) {}
          },
          onComplete: function () {
            if (manager === null) {
              fail('DIRECT_MANAGER_UNAVAILABLE', 'active App curve manager is unavailable');
              return;
            }
            var base;
            try {
              base = requestMain();
            } catch (error) {
              fail('DIRECT_REQUEST_UNAVAILABLE', error);
              return;
            }
            base.then(function (baseCurve) {
              payload.quotes.push(quote(baseCurve));
              var specs = [
                [7031, 33007, false],
                [7032, 33015, false],
                [7034, 216, true],
                [7051, 36883, false]
              ];
              var chain = Promise.resolve();
              specs.forEach(function (spec) {
                chain = chain.then(function () {
                  return requestIndicator(baseCurve, spec[0], spec[1], spec[2]).then(function (indicator) {
                    if (indicator === null) payload.missing_techids.push(spec[0]);
                    else payload.indicators.push(indicator);
                  });
                });
              });
              chain.then(function () {
                removeGroup(mainGroup);
                complete(payload);
              }).catch(function (error) {
                removeGroup(mainGroup);
                fail('DIRECT_RESPONSE_INVALID', error);
              });
            }).catch(function (error) {
              removeGroup(mainGroup);
              fail(error.code || 'DIRECT_REQUEST_FAILED', error.message || error);
            });
          }
        });
      });
    });
  }
};
"""
