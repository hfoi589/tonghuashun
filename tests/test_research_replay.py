from __future__ import annotations

from level2_service.research_replay import (
    build_replay_markers,
    calculate_replay_performance,
    calculate_macd,
)


def test_calculate_macd_uses_dynamic_settings_and_returns_rounded_rows():
    rows = calculate_macd(
        [10.0, 10.5, 11.0, 10.75],
        {"short": 2, "long": 3, "signal": 2},
    )

    assert len(rows) == 4
    assert set(rows[-1]) == {"diff", "dea", "macd"}
    assert rows[-1]["diff"] == round(rows[-1]["diff"], 6)
    assert rows[-1]["dea"] == round(rows[-1]["dea"], 6)


def test_calculate_replay_performance_buys_100_shares_and_forces_final_exit():
    result = calculate_replay_performance(
        [
            {"index": 0, "side": "buy", "price": 10.0, "time": "09:30"},
            {"index": 1, "side": "buy", "price": 11.0, "time": "09:31"},
        ],
        [{"price": 12.0, "time": "15:00"}],
    )

    assert result["buy_count"] == 2
    assert result["sell_count"] == 1
    assert result["buy_cost"] == 2100.0
    assert result["sell_proceeds"] == 2400.0
    assert result["formula_return_pct"] == (2400.0 / 2100.0 - 1) * 100
    assert result["forced_exit"] == {"time": "15:00", "price": 12.0}


def test_calculate_replay_performance_ignores_sell_without_position():
    result = calculate_replay_performance(
        [{"index": 0, "side": "sell", "price": 10.0, "time": "09:30"}],
        [{"price": 10.0, "time": "15:00"}],
    )

    assert result["buy_count"] == 0
    assert result["sell_count"] == 0
    assert result["formula_return_pct"] is None


def test_build_replay_markers_applies_code_macd_and_flow_conditions():
    points = [
        {"time": "09:30", "price": "10", "diff": -0.1, "dea": 0, "macd": -0.2, "large_order_net": "100"},
        {"time": "09:31", "price": "10.2", "diff": 0.1, "dea": 0, "macd": 0.2, "large_order_net": "120"},
    ]
    rules = [{
        "id": "buy",
        "enabled": True,
        "side": "buy",
        "title": "大单金叉",
        "joiner": "and",
        "conditions": [
            {"type": "code_filter", "codes": ["601872"]},
            {"type": "macd_signal", "signal": "red"},
            {"type": "flow_change_pct", "flow": "big", "minutes": 1, "direction": "increase", "value": 10},
        ],
    }]

    markers = build_replay_markers("601872", points, rules, marker_threshold=0.01)

    assert markers == [{
        "index": 1,
        "time": "09:31",
        "price": 10.2,
        "side": "buy",
        "rule_id": "buy",
        "rule_title": "大单金叉",
    }]
