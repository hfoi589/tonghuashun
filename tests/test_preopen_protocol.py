import pytest

from level2_service.market_data import MarketPhase
from level2_service.preopen_protocol import (
    AUCTION_PAGE_ID,
    AUCTION_PROTOCOL_ID,
    build_auction_request,
    decode_auction_quote,
    decode_market_phase,
    decode_watchlist_quote,
)


def test_auction_request_matches_apk_protocol_page_and_subscription_fields() -> None:
    assert AUCTION_PROTOCOL_ID == 1004
    assert AUCTION_PAGE_ID == 6002
    request = build_auction_request("601872", subscribe=True)
    assert "key=dpjjyd_cas_601872" in request
    assert "action=subscribe" in request
    assert "data_id_list=5" in request
    assert "stock_list=all" in request
    assert "max_msg_num=61" in request
from level2_service.parsed_values import DirectRequestError


def test_decode_market_phase_reads_app_status_and_server_time() -> None:
    state = decode_market_phase({
        "data": [
            {"dataId": 34834, "value": "CALL_AUCTION"},
            {"dataId": 1, "value": "09:16:02"},
        ],
    })

    assert state.phase is MarketPhase.CALL_AUCTION
    assert state.server_time == "09:16:02"
    assert state.source == "APP_STATUS"


def test_decode_auction_quote_uses_virtual_match_price_and_change() -> None:
    snapshot = decode_auction_quote(
        {
            "stockcode": "600000",
            "stock_name": "浦发银行",
            "bidding_direction": "10.20",
            "rise_percent": "1.23",
            "time": "09:16:02",
        },
        symbol="600000",
        market="17",
    )

    assert snapshot.source == "THS_AUCTION"
    assert snapshot.quote["price"] == "10.20"
    assert snapshot.quote["change_percent"] == "1.23%"
    assert snapshot.market_phase is MarketPhase.CALL_AUCTION


def test_decode_watchlist_quote_rejects_identity_mismatch() -> None:
    with pytest.raises(DirectRequestError, match="AUCTION_RESPONSE_INVALID"):
        decode_watchlist_quote(
            {"symbol": "000001", "name": "平安银行", "price": "10.20", "change_percent": "1.00"},
            symbol="600000",
            market="17",
        )
