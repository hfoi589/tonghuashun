import json
from datetime import datetime, timezone

import pytest

from level2_service.parsed_values import SymbolLookup, SymbolLookupNotFoundError


def test_sina_source_reads_each_node_in_100_record_pages_and_filters_market_exchange_pairs():
    from level2_service.symbol_catalog import SinaSymbolCatalogSource

    calls: list[str] = []
    rows = {
        "hs_a": [
            {"symbol": "sh600000", "code": "600000", "name": "浦发银行"},
            {"symbol": "sz300750", "code": "300750", "name": "宁德时代"},
            {"symbol": "bj920002", "code": "920002", "name": "万达轴承"},
            {"symbol": "sz600000", "code": "600000", "name": "错误交易所"},
        ],
        "etf_hq_fund": [
            {"symbol": "sh510300", "code": "510300", "name": "沪深300ETF"},
            {"symbol": "sz159919", "code": "159919", "name": "沪深300ETF"},
            {"symbol": "sh159919", "code": "159919", "name": "错误交易所"},
        ],
        "lof_hq_fund": [
            {"symbol": "sh501018", "code": "501018", "name": "南方原油"},
            {"symbol": "sz160105", "code": "160105", "name": "南方积配"},
        ],
    }

    def fetch(url: str, _timeout: float) -> str:
        calls.append(url)
        node = url.split("node=")[1].split("&", 1)[0]
        if "getHQNodeStockCount" in url:
            return str(len(rows[node]))
        page = int(url.split("page=")[1].split("&", 1)[0])
        if page > 1:
            return "[]"
        return json.dumps(rows[node], ensure_ascii=False)

    source = SinaSymbolCatalogSource(fetch=fetch)

    assert [(item.symbol, item.name, item.market) for item in source.fetch_symbols()] == [
        ("600000", "浦发银行", "17"),
        ("300750", "宁德时代", "33"),
        ("920002", "万达轴承", "151"),
        ("510300", "沪深300ETF", "20"),
        ("159919", "沪深300ETF", "36"),
        ("501018", "南方原油", "20"),
        ("160105", "南方积配", "36"),
    ]
    assert len(calls) == 6
    assert all("num=100" in url for url in calls if "getHQNodeStockCount" not in url)


def test_sina_source_keeps_paging_when_the_server_caps_a_500_request_at_100_records():
    from level2_service.symbol_catalog import SinaSymbolCatalogSource

    calls: list[str] = []
    rows = {
        "hs_a": {"symbol": "sh600000", "code": "600000", "name": "浦发银行"},
        "etf_hq_fund": {"symbol": "sh510300", "code": "510300", "name": "沪深300ETF"},
        "lof_hq_fund": {"symbol": "sh501018", "code": "501018", "name": "南方原油"},
    }

    def fetch(url: str, _timeout: float) -> str:
        calls.append(url)
        node = url.split("node=")[1].split("&", 1)[0]
        if "getHQNodeStockCount" in url:
            return "101"
        page = int(url.split("page=")[1].split("&", 1)[0])
        return json.dumps([rows[node]] * (100 if page == 1 else 1 if page == 2 else 0), ensure_ascii=False)

    source = SinaSymbolCatalogSource(fetch=fetch)

    assert [item.symbol for item in source.fetch_symbols()] == ["600000", "510300", "501018"]
    page_calls = [url for url in calls if "getHQNodeStockCount" not in url]
    assert len(page_calls) == 6
    assert all("num=100" in url for url in page_calls)


def test_refresh_activates_a_complete_version_for_exact_lookup(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog

    class Source:
        def fetch_symbols(self):
            return [
                SymbolLookup("600000", "浦发银行", "17"),
                SymbolLookup("300750", "宁德时代", "33"),
            ]

    catalog = SQLiteSymbolCatalog(
        tmp_path / "symbols.db",
        Source(),
        minimum_security_count=2,
        clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    refreshed = catalog.refresh()

    assert refreshed.version == 1
    assert refreshed.count == 2
    assert len(refreshed.checksum) == 64
    assert catalog.lookup("600000") == SymbolLookup("600000", "浦发银行", "17")
    assert catalog.status().active_version == 1


def test_refresh_deduplicates_identical_rows_and_rejects_name_conflicts(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog, SymbolCatalogError

    class Source:
        values = [
            SymbolLookup("600000", "浦发银行", "17"),
            SymbolLookup("600000", "浦发银行", "17"),
        ]

        def fetch_symbols(self):
            return self.values

    source = Source()
    catalog = SQLiteSymbolCatalog(
        tmp_path / "symbols.db",
        source,
        minimum_security_count=1,
    )

    assert catalog.refresh().count == 1
    source.values = [
        SymbolLookup("600000", "浦发银行", "17"),
        SymbolLookup("600000", "冲突名称", "17"),
    ]

    with pytest.raises(SymbolCatalogError, match="SYMBOL_CATALOG_SOURCE_INVALID"):
        catalog.refresh()


def test_exact_lookup_uses_the_existing_not_found_semantics(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog

    class Source:
        def fetch_symbols(self):
            return [SymbolLookup("600000", "浦发银行", "17")]

    catalog = SQLiteSymbolCatalog(
        tmp_path / "symbols.db",
        Source(),
        minimum_security_count=1,
    )
    catalog.refresh()

    with pytest.raises(SymbolLookupNotFoundError):
        catalog.lookup("600001")


def test_search_orders_code_prefix_before_name_match(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog

    class Source:
        def fetch_symbols(self):
            return [
                SymbolLookup("600000", "600000科技", "17"),
                SymbolLookup("600001", "浦发银行", "17"),
                SymbolLookup("300750", "600", "33"),
            ]

    catalog = SQLiteSymbolCatalog(tmp_path / "symbols.db", Source(), minimum_security_count=3)
    catalog.refresh()

    assert [item.symbol for item in catalog.search("600")] == ["600000", "600001", "300750"]


def test_refresh_rejects_a_shrunken_snapshot_without_replacing_the_active_version(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog, SymbolCatalogError

    class Source:
        values = [
            SymbolLookup(f"6000{index:02d}", f"股票{index}", "17")
            for index in range(10)
        ]

        def fetch_symbols(self):
            return self.values

    source = Source()
    catalog = SQLiteSymbolCatalog(
        tmp_path / "symbols.db",
        source,
        minimum_security_count=1,
        shrink_ratio=0.9,
    )
    catalog.refresh()
    source.values = source.values[:8]

    with pytest.raises(SymbolCatalogError, match="SYMBOL_CATALOG_SHRINK_REJECTED"):
        catalog.refresh()

    assert catalog.status().active_version == 1
    assert catalog.lookup("600000").name == "股票0"


def test_lookup_and_search_fail_closed_after_the_catalog_is_stale(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog, SymbolCatalogError

    now = [datetime(2026, 8, 27, tzinfo=timezone.utc)]

    class Source:
        def fetch_symbols(self):
            return [SymbolLookup("600000", "浦发银行", "17")]

    catalog = SQLiteSymbolCatalog(
        tmp_path / "symbols.db",
        Source(),
        minimum_security_count=1,
        clock=lambda: now[0],
    )
    catalog.refresh()
    now[0] = datetime(2026, 9, 4, tzinfo=timezone.utc)

    assert catalog.status().stale is True
    with pytest.raises(SymbolCatalogError, match="SYMBOL_CATALOG_STALE"):
        catalog.lookup("600000")
    with pytest.raises(SymbolCatalogError, match="SYMBOL_CATALOG_STALE"):
        catalog.search("浦发")


def test_search_escapes_sql_like_wildcards(tmp_path):
    from level2_service.symbol_catalog import SQLiteSymbolCatalog

    class Source:
        def fetch_symbols(self):
            return [
                SymbolLookup("600000", "百分号%%基金", "17"),
                SymbolLookup("600001", "普通股票", "17"),
            ]

    catalog = SQLiteSymbolCatalog(tmp_path / "symbols.db", Source(), minimum_security_count=2)
    catalog.refresh()

    assert [item.symbol for item in catalog.search("%%")] == ["600000"]
