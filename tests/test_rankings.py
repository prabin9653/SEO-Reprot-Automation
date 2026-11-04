import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seo_report.rankings import (
    MISSING,
    NOT_RECORDED,
    Ranking,
    RankingError,
    RankingKind,
    movement,
    parse_ranking,
)


def position(n):
    return Ranking(RankingKind.POSITION, position=n)


@pytest.mark.parametrize(
    "value, expected",
    [
        (16, 16),
        (1, 1),
        (16.0, 16),
        ("16", 16),
        ("  7 ", 7),
        ("Page 2, Rank 6", 16),
        ("page 1, rank 1", 1),
        ("PAGE 3 , RANK 10", 30),
        ("Page 2 Rank 6", 16),
    ],
)
def test_numeric_positions(value, expected):
    ranking = parse_ranking(value)
    assert ranking.kind is RankingKind.POSITION
    assert ranking.position == expected


@pytest.mark.parametrize("value", ["Above 10", "above 10", "  ABOVE 10  "])
def test_above_10_is_non_numeric_and_preserved(value):
    ranking = parse_ranking(value)
    assert ranking.kind is RankingKind.ABOVE_10
    assert ranking.position is None
    assert not ranking.is_numeric
    assert ranking.page_rank() is None
    assert ranking.display() == value.strip()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_is_missing_not_zero(value):
    ranking = parse_ranking(value)
    assert ranking is MISSING
    assert ranking.position is None
    assert ranking.display() == NOT_RECORDED


@pytest.mark.parametrize(
    "value",
    [
        0,
        -3,
        "0",
        16.5,
        float("nan"),
        True,
        "abc",
        "N/A",
        "Above 20",
        "Above  10",
        "Page 2",
        "Page 0, Rank 1",
        "Page 2, Rank 0",
        "Page 2, Rank 11",
        "16.5",
        ["16"],
    ],
)
def test_invalid_values_raise_with_location(value):
    location = "'Keyword Ranking Sheet' row 5, column 'Current Ranking'"
    with pytest.raises(RankingError) as error:
        parse_ranking(value, location)
    assert location in str(error.value)
    assert repr(value) in str(error.value)


def test_page_rank_derived_from_position():
    assert position(16).page_rank() == (2, 6)
    assert position(1).page_rank() == (1, 1)
    assert position(10).page_rank() == (1, 10)
    assert position(11).page_rank() == (2, 1)


def test_movement_positive_when_improved():
    assert movement(position(8), position(3)) == 5
    assert movement(position(3), position(8)) == -5
    assert movement(position(4), position(4)) == 0


@pytest.mark.parametrize(
    "previous, current",
    [
        (position(8), parse_ranking("Above 10")),
        (parse_ranking("Above 10"), position(8)),
        (parse_ranking("Above 10"), parse_ranking("above 10")),
        (position(8), MISSING),
        (MISSING, position(8)),
        (MISSING, MISSING),
    ],
)
def test_no_movement_unless_both_numeric(previous, current):
    assert movement(previous, current) is None
