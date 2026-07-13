"""Normalize the EBBs' many date formats to sortable ISO at ingest."""
from __future__ import annotations

import html
import re
from datetime import datetime

_FORMATS = (
    "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S%p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%m/%d/%y %H:%M", "%m/%d/%y",
    "%d-%b-%y", "%d-%b-%Y",
    "%m-%d-%Y %H:%M", "%m-%d-%Y",
)


def to_iso(raw: str | None) -> str:
    """Best-effort ISO 8601 ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM').

    Returns '' for empty/HTML-junk input and for open-ended sentinels
    (12/31/9000-style). Returns the input unchanged if no format matches,
    so unparseable postings are never silently lost.
    """
    s = html.unescape(raw or "").replace("\xa0", " ").strip()
    s = " ".join(s.split())
    if not s:
        return ""
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.year >= 9000:  # "no end date" sentinel
            return ""
        if dt.hour or dt.minute:
            return dt.strftime("%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%d")
    return s


def month_day_range(raw: str, year: int) -> tuple[str, str]:
    """Parse CGT foghorn-style ranges: 'July 06 - 10', 'July 20',
    'July 28 - August 03'. Returns (start_iso, end_iso) or ('', '')."""
    s = " ".join((raw or "").split())
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*-\s*(?:([A-Za-z]+)\s+)?(\d{1,2}))?$", s)
    if not m:
        return "", ""
    mon1, d1, mon2, d2 = m.groups()
    try:
        start = datetime.strptime(f"{mon1} {d1} {year}", "%B %d %Y")
        end = datetime.strptime(f"{mon2 or mon1} {d2 or d1} {year}", "%B %d %Y")
    except ValueError:
        return "", ""
    if end < start:  # range wraps the year end
        end = end.replace(year=year + 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
