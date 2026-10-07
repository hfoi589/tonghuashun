"""Versioned local calendar for Chinese statutory workdays."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ChinaLegalWorkdayCalendar:
    coverage_start: date
    coverage_end: date
    holidays: frozenset[date]
    adjusted_workdays: frozenset[date]

    @classmethod
    def from_file(cls, path: str | Path) -> "ChinaLegalWorkdayCalendar":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported China workday calendar format")
        start = date.fromisoformat(str(payload["coverage_start"]))
        end = date.fromisoformat(str(payload["coverage_end"]))
        if end < start:
            raise ValueError("China workday calendar coverage is invalid")
        holidays = frozenset(date.fromisoformat(str(value)) for value in payload["holidays"])
        adjusted = frozenset(date.fromisoformat(str(value)) for value in payload["adjusted_workdays"])
        if holidays & adjusted:
            raise ValueError("a date cannot be both a holiday and adjusted workday")
        return cls(start, end, holidays, adjusted)

    def is_workday(self, value: date) -> bool | None:
        if value < self.coverage_start or value > self.coverage_end:
            return None
        if value in self.holidays:
            return False
        if value in self.adjusted_workdays:
            return True
        return value.weekday() < 5

    def add_workdays(self, start: date, count: int) -> date | None:
        if count < 0:
            raise ValueError("workday count cannot be negative")
        if self.is_workday(start) is None:
            return None
        current = start
        remaining = count
        while remaining:
            current += timedelta(days=1)
            working = self.is_workday(current)
            if working is None:
                return None
            if working:
                remaining -= 1
        return current

    def expiry_date(self, updated_at: datetime | None) -> date | None:
        if updated_at is None or updated_at.tzinfo is None:
            return None
        local_date = updated_at.astimezone(SHANGHAI_TZ).date()
        return self.add_workdays(local_date, 7)


def default_china_workday_calendar() -> ChinaLegalWorkdayCalendar:
    return ChinaLegalWorkdayCalendar.from_file(Path(__file__).with_name("china_workdays.json"))
