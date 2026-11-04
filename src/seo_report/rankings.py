"""Parsing and comparison of manually maintained keyword rankings."""

import math
import re
from dataclasses import dataclass
from enum import Enum

RESULTS_PER_PAGE = 10
ABOVE_10_LABEL = "above 10"
NOT_RECORDED = "Not recorded"

_PLAIN_INT_RE = re.compile(r"\d+", re.ASCII)
_PAGE_RANK_RE = re.compile(r"page\s*(\d+)\s*,?\s*rank\s*(\d+)", re.IGNORECASE | re.ASCII)


class RankingError(ValueError):
    """A ranking cell holds a value that cannot be interpreted."""


class RankingKind(Enum):
    POSITION = "position"
    ABOVE_10 = "above_10"
    MISSING = "missing"


@dataclass(frozen=True)
class Ranking:
    kind: RankingKind
    position: int | None = None
    text: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.kind is RankingKind.POSITION

    def page_rank(self) -> tuple[int, int] | None:
        """Return (page, rank within page) for numeric positions, else None."""
        if self.position is None:
            return None
        return (self.position - 1) // RESULTS_PER_PAGE + 1, (self.position - 1) % RESULTS_PER_PAGE + 1

    def display(self) -> str:
        if self.kind is RankingKind.MISSING:
            return NOT_RECORDED
        if self.kind is RankingKind.ABOVE_10:
            return self.text
        return str(self.position)


MISSING = Ranking(RankingKind.MISSING)


def _invalid(value, location: str, reason: str) -> RankingError:
    return RankingError(f"{location}: invalid ranking value {value!r} ({reason})")


def _position(number: int, value, location: str) -> Ranking:
    if number < 1:
        raise _invalid(value, location, "position must be 1 or greater")
    return Ranking(RankingKind.POSITION, position=number)


def parse_ranking(value, location: str = "ranking value") -> Ranking:
    """Parse one Previous/Current Ranking cell.

    Accepts a positive integer position, "Page N, Rank M", the label "Above 10"
    (case-insensitive, preserved as written) or a blank cell. Anything else raises
    RankingError that names `location` and the offending value.
    """
    if value is None:
        return MISSING
    if isinstance(value, bool):
        raise _invalid(value, location, "expected a position, 'Page N, Rank M' or 'Above 10'")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
            raise _invalid(value, location, "position must be a whole number")
        return _position(int(value), value, location)
    if not isinstance(value, str):
        raise _invalid(value, location, "unsupported cell type")

    text = value.strip()
    if not text:
        return MISSING
    if text.casefold() == ABOVE_10_LABEL:
        return Ranking(RankingKind.ABOVE_10, text=text)
    if _PLAIN_INT_RE.fullmatch(text):
        return _position(int(text), value, location)

    match = _PAGE_RANK_RE.fullmatch(text)
    if match:
        page, rank = int(match.group(1)), int(match.group(2))
        if page < 1:
            raise _invalid(value, location, "page must be 1 or greater")
        if not 1 <= rank <= RESULTS_PER_PAGE:
            raise _invalid(value, location, f"rank must be between 1 and {RESULTS_PER_PAGE}")
        return Ranking(RankingKind.POSITION, position=(page - 1) * RESULTS_PER_PAGE + rank)

    raise _invalid(value, location, "expected a position, 'Page N, Rank M' or 'Above 10'")


def movement(previous: Ranking, current: Ranking) -> int | None:
    """Positions gained (positive) or lost (negative); None unless both are numeric."""
    if previous.is_numeric and current.is_numeric:
        return previous.position - current.position
    return None
