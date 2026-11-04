import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seo_report.periods import Period, PeriodError, report_periods

D = dt.date


def test_period_is_inclusive_on_both_ends():
    period = Period(D(2026, 9, 29), D(2026, 10, 26))
    assert period.days == 28
    dates = period.dates()
    assert dates[0] == D(2026, 9, 29)
    assert dates[-1] == D(2026, 10, 26)
    assert len(dates) == 28
    assert D(2026, 9, 29) in period and D(2026, 10, 26) in period
    assert D(2026, 9, 28) not in period and D(2026, 10, 27) not in period


def test_single_day_period():
    assert Period(D(2026, 1, 1), D(2026, 1, 1)).days == 1


def test_comparison_is_the_28_days_directly_before_start():
    periods = report_periods(D(2026, 9, 29), D(2026, 10, 26))
    assert (periods.current.start, periods.current.end) == (D(2026, 9, 29), D(2026, 10, 26))
    assert (periods.previous.start, periods.previous.end) == (D(2026, 9, 1), D(2026, 9, 28))
    assert periods.previous.days == 28
    assert periods.previous.end + dt.timedelta(days=1) == periods.current.start


def test_comparison_across_month_and_leap_year_boundaries():
    periods = report_periods(D(2026, 3, 5), D(2026, 4, 1))
    assert (periods.previous.start, periods.previous.end) == (D(2026, 2, 5), D(2026, 3, 4))
    leap = report_periods(D(2028, 3, 5), D(2028, 4, 1))
    assert (leap.previous.start, leap.previous.end) == (D(2028, 2, 6), D(2028, 3, 4))
    assert leap.previous.days == 28


def test_explicit_length_is_enforced_for_both_periods():
    periods = report_periods(D(2026, 10, 1), D(2026, 10, 7), days=7)
    assert periods.previous.days == 7
    assert periods.previous.start == D(2026, 9, 24)


@pytest.mark.parametrize("end", [D(2026, 10, 25), D(2026, 10, 27)])
def test_wrong_length_is_rejected(end):
    with pytest.raises(PeriodError, match="exactly 28 days"):
        report_periods(D(2026, 9, 29), end)


def test_end_before_start_is_rejected():
    with pytest.raises(PeriodError, match="before its start"):
        Period(D(2026, 10, 26), D(2026, 9, 29))


@pytest.mark.parametrize(
    "start, end",
    [
        ("2026-09-29", D(2026, 10, 26)),
        (D(2026, 9, 29), None),
        (dt.datetime(2026, 9, 29, 12), D(2026, 10, 26)),
    ],
)
def test_non_date_values_are_rejected(start, end):
    with pytest.raises(PeriodError, match="must be a date"):
        Period(start, end)
