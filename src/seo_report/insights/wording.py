"""Number, date and phrase formatting for report text (US English).

Pure helpers with no I/O and no clock. Rounding is half-up so that output is identical on
every run. Month names are fixed English strings, so results do not depend on the locale.
"""

import re
from decimal import ROUND_HALF_UP, Decimal

from seo_report.metrics import Change, Direction

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
EN_DASH = "–"

BRITISH_TO_US = {
    "optimisation": "optimization", "optimise": "optimize", "optimised": "optimized",
    "optimises": "optimizes", "optimising": "optimizing", "colour": "color",
    "behaviour": "behavior", "centre": "center", "analyse": "analyze", "analysed": "analyzed",
    "organisation": "organization", "favour": "favor", "programme": "program",
    "localisation": "localization", "prioritise": "prioritize", "prioritised": "prioritized",
    "specialise": "specialize", "licence": "license", "whilst": "while",
    "enquiry": "inquiry", "enquiries": "inquiries",
}

# Words the report must not use without evidence; the engine's text never includes them.
UNSUPPORTED_SUPERLATIVES = (
    "strong", "strongly", "excellent", "outstanding", "impressive", "remarkable", "best",
    "dominant", "dominate", "skyrocket", "skyrocketed", "soar", "soared", "explosive",
    "massive", "huge", "record", "great", "amazing", "tremendous", "stellar", "exceptional",
)

# Words that imply growth; they must not appear next to a metric that fell.
GROWTH_WORDS = (
    "growth", "growing", "grew", "grow", "increase", "increased", "increasing", "improved",
    "improvement", "improving", "gain", "gained", "gains", "rise", "rose", "rising",
    "climbed", "climbing", "boost", "boosted", "surge", "surged", "up from",
)

_MONTH_ALT = "|".join(MONTHS)
_DATE_RE = re.compile(
    rf"(?:{_MONTH_ALT})\s+\d{{1,2}}(?:\s*[–-]\s*(?:(?:{_MONTH_ALT})\s+)?\d{{1,2}})?(?:,\s*\d{{4}})?"
)
_QUOTED_RE = re.compile(r"“[^”]*”")
_NUMBER_RE = re.compile(r"(?<![\w/])\d[\d,]*(?:\.\d+)?(?:[KM]\b|%)?")


def _round(value, decimals: int) -> Decimal:
    exponent = Decimal(1).scaleb(-decimals)
    return Decimal(str(round(float(value), 10))).quantize(exponent, rounding=ROUND_HALF_UP)


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_count(value) -> str:
    """1862 -> '1,862'."""
    return f"{int(_round(value, 0)):,}"


def format_compact(value) -> str:
    """1960 -> '1.96K', matching how Search Console shows large totals."""
    sign = "-" if value < 0 else ""
    n = abs(value)
    if n < 1000:
        return sign + format_count(n)
    thousands = _round(n / 1000, 2)
    if thousands < 1000:
        return f"{sign}{_plain(thousands)}K"
    return f"{sign}{_plain(_round(n / 1_000_000, 2))}M"


def format_rate(fraction) -> str:
    """0.0039 -> '0.39%', 0.311 -> '31.1%' (two decimals below 10%, one above)."""
    percent = float(fraction) * 100
    decimals = 2 if abs(percent) < 10 else 1
    return f"{_plain(_round(percent, decimals))}%"


def format_percent_change(fraction) -> str:
    """Magnitude of a relative change as text: -0.3178 -> '31.8%'."""
    percent = abs(float(fraction)) * 100
    return f"{_plain(_round(percent, 0 if percent >= 100 else 1))}%"


def format_points(difference) -> str:
    """Magnitude of a difference between two rates, in percentage points: 0.0021 -> '0.21'."""
    points = abs(float(difference)) * 100
    return _plain(_round(points, 2 if points < 1 else 1))


def format_position(value) -> str:
    """Search Console average position with one decimal: 29.6."""
    return format(_round(value, 1), "f")


def format_date(day) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def format_date_range(start, end) -> str:
    """Inclusive range as text, e.g. 'September 29–October 26, 2026'."""
    if end < start:
        raise ValueError("range end is before its start")
    if start == end:
        return format_date(start)
    if start.year != end.year:
        return f"{format_date(start)}{EN_DASH}{format_date(end)}"
    if start.month == end.month:
        return f"{MONTHS[start.month - 1]} {start.day}{EN_DASH}{end.day}, {start.year}"
    return f"{MONTHS[start.month - 1]} {start.day}{EN_DASH}{MONTHS[end.month - 1]} {end.day}, {start.year}"


def count_noun(value, singular: str, plural: str | None = None, *, compact: bool = False) -> str:
    """6 -> '6 clicks', 1 -> '1 click', 1862 -> '1,862 impressions'."""
    word = singular if value == 1 else (plural or singular + "s")
    return f"{format_compact(value) if compact else format_count(value)} {word}"


def join_list(items) -> str:
    """['a', 'b', 'c'] -> 'a, b, and c' (US serial comma)."""
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def quoted(text) -> str:
    return f"“{text}”"


def capitalize_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def ensure_sentence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if not text or text[-1] in ".!?" else text + "."


def join_sentences(sentences) -> str:
    return " ".join(ensure_sentence(s) for s in sentences if s and str(s).strip())


def word_count(text: str) -> int:
    return len(str(text).split())


# --- change phrases (previous-period wording) -----------------------------------------------

def _unchanged(change: Change, previous: str) -> str:
    return f"{'unchanged' if change.absolute == 0 else 'essentially unchanged'} from {previous}"


def change_phrase(change: Change, fmt=format_count) -> str | None:
    """Phrase for a count: 'up from 4', 'down 31.8% from 2.28K', 'essentially unchanged from 105'.

    Counts with a small previous base get absolute wording only (no percentage).
    Returns None when the change is not comparable.
    """
    if not change.comparable:
        return None
    previous = fmt(change.previous)
    if change.direction is Direction.FLAT:
        return _unchanged(change, previous)
    word = "up" if change.direction is Direction.UP else "down"
    if change.percent is None:
        return f"{word} from {previous}"
    return f"{word} {format_percent_change(change.percent)} from {previous}"


def rate_change_phrase(change: Change) -> str | None:
    """Phrase for a rate (CTR, engagement, share): 'up from 0.18%'."""
    if not change.comparable:
        return None
    previous = format_rate(change.previous)
    if change.direction is Direction.FLAT:
        return _unchanged(change, previous)
    return f"{'up' if change.direction is Direction.UP else 'down'} from {previous}"


def position_change_phrase(change: Change) -> str | None:
    """Phrase for Search Console average position, where a lower number is better.

    A worse position is described neutrally ('compared with 31.0'), never as growth.
    """
    if not change.comparable:
        return None
    previous = format_position(change.previous)
    if change.direction is Direction.FLAT:
        return _unchanged(change, previous)
    if change.direction is Direction.DOWN:
        return f"improved from {previous}"
    return f"compared with {previous}"


# --- text checks -----------------------------------------------------------------------------

def _words_found(text: str, words) -> list[str]:
    lowered = str(text).casefold()
    return sorted({w for w in words if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", lowered)})


def find_non_us_spelling(text: str) -> list[str]:
    """British spellings found in text (case-insensitive, whole words)."""
    return _words_found(text, BRITISH_TO_US)


def find_superlatives(text: str) -> list[str]:
    return _words_found(text, UNSUPPORTED_SUPERLATIVES)


def find_growth_words(text: str) -> list[str]:
    return _words_found(text, GROWTH_WORDS)


def numbers_in(text: str) -> list[str]:
    """Numeric tokens in generated text, for checking them against evidence.

    Dates written by format_date/format_date_range and quoted labels (“…”) are ignored.
    Thousands separators are removed, so '1,862' becomes '1862'; '%', 'K' and 'M' suffixes
    are kept.
    """
    cleaned = _QUOTED_RE.sub(" ", str(text))
    cleaned = _DATE_RE.sub(" ", cleaned)
    return [token.replace(",", "") for token in _NUMBER_RE.findall(cleaned)]
