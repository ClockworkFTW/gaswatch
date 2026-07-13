"""Currently-effective rates view over the rate_docs change-tracking table.

Pipelines publish rates as documents (rate matrices, index-rate postings,
statements of rates inside tariff PDFs), not machine-readable values. This
module turns the tracked documents into a "what applies right now" view:

- only documents still present on the EBB (seen by the latest rates pull),
- an effective date parsed from the title where one is stated,
- within a document series (same title minus the date), the newest document
  whose effective date has arrived wins; older ones are superseded and
  not-yet-effective ones are flagged as pending.

Documents without a parseable date (entire tariffs, rate-schedule sections,
GT&C) are standing documents and always count as current.
"""
from __future__ import annotations

import html
import re
import sqlite3
from datetime import date

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}

_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
             r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|"
             r"nov(?:ember)?|dec(?:ember)?)")

# ordered: most specific first; each yields (iso_date, matched_span)
_PATTERNS = [
    # 4/1/2023, 04.01.2026
    (re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})(?!\d)"),
     lambda m: (_year(m.group(3)), int(m.group(1)), int(m.group(2)))),
    # June 1, 2026
    (re.compile(_MONTH_RE + r"\.?\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE),
     lambda m: (int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))),
    # October 2025 / October, 2020
    (re.compile(_MONTH_RE + r"\.?,?\s+(\d{4})", re.IGNORECASE),
     lambda m: (int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], 1)),
    # 2026-06 / 2026-06-15
    (re.compile(r"(?<!\d)(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?!\d)"),
     lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))),
    # bare year: rates2026, "2017 Table of Effective Rates"
    (re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)"),
     lambda m: (int(m.group(1)), 1, 1)),
]

# qualifiers that mark a replacement posting, not a distinct series
_SERIES_NOISE = re.compile(r"\b(amended|revised|updated|final)\b", re.IGNORECASE)


def _year(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


def parse_effective(title: str) -> tuple[str | None, str]:
    """Extract (ISO effective date | None, series key) from a document title.

    The series key is the title with the date text, replacement qualifiers,
    and punctuation removed — documents sharing a key are versions of the
    same posting.
    """
    text = html.unescape(title)
    effective, span = None, None
    for pattern, build in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            y, mo, d = build(m)
            effective = date(y, mo, d).isoformat()
        except ValueError:
            continue
        if not (1990 <= y <= 2100):
            effective = None
            continue
        span = m.span()
        break
    stripped = text if span is None else text[:span[0]] + text[span[1]:]
    stripped = _SERIES_NOISE.sub(" ", stripped)
    key = re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()
    return effective, key


def effective_rate_docs(conn: sqlite3.Connection, pipeline: str | None = None,
                        include_superseded: bool = False,
                        today: date | None = None) -> list[dict]:
    """Rows for the currently-effective-rates view, sorted for display.

    Each row: pipeline, doc_type, title, url, effective (ISO or ''),
    status ('current' | 'pending' | 'superseded'), changed_at, first_seen.
    Superseded rows are omitted unless include_superseded is set.
    """
    today = today or date.today()
    where, params = "", []
    if pipeline:
        where = " AND r.pipeline = ?"
        params.append(pipeline.lower())
    # only documents the latest rates pull still saw on the EBB
    rows = conn.execute(f"""
        SELECT r.pipeline, r.doc_type, r.title, r.url, r.changed_at, r.first_seen
        FROM rate_docs r
        JOIN (SELECT pipeline, MAX(last_seen) AS ls FROM rate_docs GROUP BY pipeline) m
          ON m.pipeline = r.pipeline AND r.last_seen = m.ls
        WHERE 1=1{where}
        ORDER BY r.pipeline, r.doc_type, r.first_seen DESC
    """, params).fetchall()  # noqa: S608 — where clause is parameterized

    docs = []
    for pipe, doc_type, title, url, changed_at, first_seen in rows:
        eff, key = parse_effective(title)
        docs.append({
            "pipeline": pipe, "doc_type": doc_type,
            "title": html.unescape(title).replace("\xa0", " "), "url": url,
            "effective": eff or "", "series": key,
            "changed_at": changed_at or "", "first_seen": first_seen,
        })

    # newest arrived date wins per (pipeline, doc_type, series)
    winners: dict[tuple, str] = {}
    for d in docs:
        if d["effective"] and d["effective"] <= today.isoformat():
            gk = (d["pipeline"], d["doc_type"], d["series"])
            if d["effective"] > winners.get(gk, ""):
                winners[gk] = d["effective"]
    for d in docs:
        if not d["effective"]:
            d["status"] = "current"           # standing document
        elif d["effective"] > today.isoformat():
            d["status"] = "pending"
        elif d["effective"] == winners.get((d["pipeline"], d["doc_type"], d["series"])):
            d["status"] = "current"
        else:
            d["status"] = "superseded"

    if not include_superseded:
        docs = [d for d in docs if d["status"] != "superseded"]
    docs.sort(key=lambda d: (d["pipeline"], d["doc_type"],
                             d["effective"] or "0000", d["title"]))
    return docs
