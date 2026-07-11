"""Small standard five-field cron parser with timezone-aware iteration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

_LIMITS: Final = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _parse_field(text: str, low: int, high: int, *, dow: bool = False) -> frozenset[int]:
    if not text:
        raise ValueError("empty cron field")
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        try:
            step = int(step_text) if slash else 1
        except ValueError as exc:
            raise ValueError(f"invalid cron step: {part!r}") from exc
        if step <= 0:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"invalid cron range: {part!r}") from exc
            if start > end:
                raise ValueError(f"descending cron range: {part!r}")
        else:
            if slash:
                try:
                    start = int(base)
                except ValueError as exc:
                    raise ValueError(f"invalid cron field: {part!r}") from exc
                end = high
            else:
                try:
                    start = end = int(base)
                except ValueError as exc:
                    raise ValueError(f"invalid cron field: {part!r}") from exc
        if start < low or end > high:
            raise ValueError(f"cron value outside {low}-{high}: {part!r}")
        values.update(range(start, end + 1, step))
    if dow and 7 in values:
        values.remove(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronExpression:
    expression: str
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    days: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    weekdays: frozenset[int] = frozenset()
    day_wildcard: bool = False
    weekday_wildcard: bool = False

    def __post_init__(self) -> None:
        fields = self.expression.split()
        if len(fields) != 5:
            raise ValueError("cron expression must contain exactly five fields")
        parsed = [
            _parse_field(field, low, high, dow=index == 4)
            for index, (field, (low, high)) in enumerate(zip(fields, _LIMITS, strict=True))
        ]
        object.__setattr__(self, "minutes", parsed[0])
        object.__setattr__(self, "hours", parsed[1])
        object.__setattr__(self, "days", parsed[2])
        object.__setattr__(self, "months", parsed[3])
        object.__setattr__(self, "weekdays", parsed[4])
        object.__setattr__(self, "day_wildcard", fields[2] == "*")
        object.__setattr__(self, "weekday_wildcard", fields[4] == "*")

    def matches(self, local: datetime) -> bool:
        if (
            local.minute not in self.minutes
            or local.hour not in self.hours
            or local.month not in self.months
        ):
            return False
        day_matches = local.day in self.days
        cron_weekday = (local.weekday() + 1) % 7
        weekday_matches = cron_weekday in self.weekdays
        if self.day_wildcard:
            return weekday_matches
        if self.weekday_wildcard:
            return day_matches
        return day_matches or weekday_matches

    def next_after(self, after: datetime, zone: ZoneInfo) -> datetime:
        return self._scan(after, zone, direction=1, inclusive=False)

    def previous_or_at(self, value: datetime, zone: ZoneInfo) -> datetime:
        return self._scan(value, zone, direction=-1, inclusive=True)

    def _scan(
        self,
        value: datetime,
        zone: ZoneInfo,
        *,
        direction: int,
        inclusive: bool,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cron reference time must include a timezone")
        utc_value = value.astimezone(UTC)
        candidate = utc_value.replace(second=0, microsecond=0)
        if not inclusive or (direction > 0 and candidate <= utc_value):
            candidate += timedelta(minutes=direction)
        elif direction < 0 and candidate > utc_value:
            candidate -= timedelta(minutes=1)
        limit = 8 * 366 * 24 * 60
        for _ in range(limit):
            if self.matches(candidate.astimezone(zone)):
                return candidate
            candidate += timedelta(minutes=direction)
        raise ValueError("cron expression has no occurrence within eight years")

    def next_times(self, after: datetime, zone: ZoneInfo, count: int) -> tuple[datetime, ...]:
        if count < 0 or count > 1000:
            raise ValueError("count must be between 0 and 1000")
        result: list[datetime] = []
        cursor = after
        for _ in range(count):
            cursor = self.next_after(cursor, zone)
            result.append(cursor)
        return tuple(result)
