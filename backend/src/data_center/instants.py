"""One ISO-8601 instant parser for every interpreter this platform supports.

``datetime.fromisoformat`` only learned the ``Z`` suffix in Python 3.11, while
3.10 is still supported and the console submits instants exactly as
``Date.toISOString()`` renders them — always ending in ``Z``.  Hosted CI caught
the difference the hard way: the scheduler and the plan API accepted those
instants on 3.11/3.12 and rejected them on 3.10.

Routing stored and submitted timestamps through one parser keeps the three
interpreters, the API and the scheduler honest about the same instant.  The
module deliberately has no dependencies beyond ``datetime``.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_instant(value: str | datetime) -> datetime:
    """Parse an ISO-8601 instant; a trailing ``Z`` means UTC everywhere."""
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def aware_utc(value: str | datetime, *, field: str = "timestamp") -> datetime:
    """Parse and require a timezone, returning the same instant in UTC (spec 5.1).

    A naive timestamp is a defect rather than a local time: the platform stores
    and compares UTC, so guessing a zone here would silently move data.
    """
    parsed = parse_instant(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def optional_instant(value: str | datetime | None) -> datetime | None:
    """``None`` stays ``None``; anything else is parsed as an instant."""
    return None if value is None else parse_instant(value)
