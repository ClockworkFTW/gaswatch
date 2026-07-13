"""Helpers shared by the pipeline adapters.

Every EBB posts the same concepts in a slightly different dress — number
formats, cycle labels, HTML grids, multi-MB tariff PDFs. These helpers keep
the adapters down to what is genuinely pipeline-specific.
"""
from __future__ import annotations

import io
import re
from datetime import date, timedelta

from ..http import EbbClient


def num(value) -> float | None:
    """Parse a posted quantity to float; '', '-', 'N/A', 'TBD' etc. -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def day_range(gas_day: date, start: date | None, end: date | None) -> list[date]:
    """The gas days a fetch covers: start..end inclusive when backfilling,
    else just gas_day."""
    if start and end:
        return [start + timedelta(n) for n in range((end - start).days + 1)]
    return [gas_day]


_CYCLE_KEYS = (
    ("intra day 3", "id3"), ("intraday 3", "id3"), ("id3", "id3"),
    ("intra day 2", "id2"), ("intraday 2", "id2"), ("id2", "id2"),
    ("intra day 1", "id1"), ("intraday 1", "id1"), ("id1", "id1"),
    ("evening", "evening"), ("timely", "timely"),
)


def cycle_slug(raw: str) -> str | None:
    """Map a free-text cycle label ('Evening Schedule', 'INTRA DAY 2', ...)
    to the shared cycle vocabulary; None when unrecognized (the caller picks
    its own fallback)."""
    s = (raw or "").lower()
    for key, slug in _CYCLE_KEYS:
        if key in s:
            return slug
    return None


def row_cells(tr) -> list[str]:
    """Whitespace-normalized text of each <td> in an lxml table row."""
    return [re.sub(r"\s+", " ", td.text_content()).strip() for td in tr.xpath("./td")]


def clean_body(text: str, limit: int = 30000) -> str:
    """Normalize a notice body: collapse blank-line runs, trim, cap length."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def pdf_body_text(content: bytes) -> str:
    """All page text of a (notice) PDF, normalized like the HTML bodies."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    return clean_body("\n".join(page.extract_text() or "" for page in reader.pages))


def pdf_links(html: str, base: str = "") -> list[tuple[str, str]]:
    """(absolute_url, link_text) for every PDF link on an HTML page."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', html,
                         re.IGNORECASE | re.DOTALL):
        href, text = m.groups()
        if href.startswith("/") and base:
            href = base + href
        out.append((href, re.sub(r"<[^>]+>|\s+", " ", text).strip()))
    return out


def probe_content_hash(client: EbbClient, url: str) -> str:
    """Version identity for a large document without downloading it: a 1-byte
    ranged GET, keyed on total size + Last-Modified (some servers reject HEAD)."""
    probe = client.get(url, headers={"Range": "bytes=0-0"}, ok_statuses=(206, 200))
    total = (probe.headers.get("Content-Range", "").split("/")[-1]
             or probe.headers.get("Content-Length", ""))
    return f"{total}:{probe.headers.get('Last-Modified', '')}"
