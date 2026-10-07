from __future__ import annotations

import datetime as dt

from level2_service.research_premium import (
    align_factor_closes,
    calculate_premium_rate,
    calculate_proxy_estimate,
    premium_push_slot,
)


def test_premium_calculation_and_proxy_alignment():
    assert calculate_premium_rate(2.119, 1.8709) == 2.119 / 1.8709 - 1
    aligned = align_factor_closes(
        [{"date": "2026-08-28", "close": 100}, {"date": "2026-08-31", "close": 102}],
        [{"date": "2026-08-28", "close": 7.1}, {"date": "2026-08-31", "close": 7.2}],
    )
    estimate = calculate_proxy_estimate(1.0, aligned["xop"][1]["close"], aligned["xop"][0]["close"], aligned["fx"][1]["close"], aligned["fx"][0]["close"])
    assert estimate["estimate"] == round(102 / 100 * 7.2 / 7.1, 6)


def test_premium_push_slot_matches_opm_schedule():
    timezone = dt.timezone(dt.timedelta(hours=8))
    assert premium_push_slot(dt.datetime(2026, 8, 31, 9, 10, tzinfo=timezone)) == "09:10"
    assert premium_push_slot(dt.datetime(2026, 8, 31, 9, 31, tzinfo=timezone)) == "09:31"
    assert premium_push_slot(dt.datetime(2026, 8, 31, 10, 1, tzinfo=timezone)) == "10:01"
    assert premium_push_slot(dt.datetime(2026, 8, 31, 12, 0, tzinfo=timezone)) is None
