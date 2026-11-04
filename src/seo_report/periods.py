"""Report periods: the selected period and the 28 days immediately before it."""

import datetime as dt
from dataclasses import dataclass

PERIOD_DAYS = 28


class PeriodError(ValueError):
    """A reporting period is malformed."""


@dataclass(frozen=True)
class Period:
    """A range of calendar days; both start and end are inclusive."""

    start: dt.date
    end: dt.date

    def __post_init__(self):
        for name in ("start", "end"):
            value = getattr(self, name)
            if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
                raise PeriodError(f"{name} must be a date, got {value!r}")
        if self.end < self.start:
            raise PeriodError(f"period end {self.end} is before its start {self.start}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> list[dt.date]:
        return [self.start + dt.timedelta(days=i) for i in range(self.days)]

    def __contains__(self, day) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class ReportPeriods:
    current: Period
    previous: Period


def report_periods(start: dt.date, end: dt.date, days: int = PERIOD_DAYS) -> ReportPeriods:
    """Return the report period and the `days` calendar days directly before its start."""
    current = Period(start, end)
    if current.days != days:
        raise PeriodError(
            f"report period must be exactly {days} days, got {current.days} ({start} to {end})"
        )
    previous = Period(start - dt.timedelta(days=days), start - dt.timedelta(days=1))
    return ReportPeriods(current, previous)
