from datetime import date, datetime, timezone
import json

from level2_service.workday_calendar import ChinaLegalWorkdayCalendar


def create_calendar(tmp_path, *, start, end, holidays=(), adjusted_workdays=()):
    path = tmp_path / "workdays.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "coverage_start": start,
                "coverage_end": end,
                "holidays": list(holidays),
                "adjusted_workdays": list(adjusted_workdays),
            }
        ),
        encoding="utf-8",
    )
    return ChinaLegalWorkdayCalendar.from_file(path)


def test_add_workdays_skips_holiday_and_counts_an_adjusted_weekend(tmp_path):
    calendar = create_calendar(
        tmp_path,
        start="2026-02-13",
        end="2026-03-02",
        holidays=(
            "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
            "2026-02-20", "2026-02-23",
        ),
        adjusted_workdays=("2026-02-14", "2026-02-28"),
    )

    assert calendar.add_workdays(date(2026, 2, 13), 7) == date(2026, 3, 2)


def test_add_workdays_crosses_year_and_stops_at_calendar_coverage(tmp_path):
    calendar = create_calendar(
        tmp_path,
        start="2026-12-28",
        end="2027-01-08",
        holidays=("2027-01-01",),
    )

    assert calendar.add_workdays(date(2026, 12, 28), 7) == date(2027, 1, 7)
    assert calendar.add_workdays(date(2027, 1, 7), 7) is None


def test_expiry_uses_the_shanghai_date_after_timestamp_and_requires_coverage(tmp_path):
    calendar = create_calendar(
        tmp_path,
        start="2026-08-26",
        end="2026-09-08",
    )

    assert calendar.expiry_date(datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)) == date(2026, 9, 4)
    assert calendar.expiry_date(None) is None
    assert calendar.expiry_date(datetime(2027, 1, 1, tzinfo=timezone.utc)) is None
