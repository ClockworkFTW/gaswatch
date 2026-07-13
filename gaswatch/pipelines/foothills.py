"""Foothills Pipe Lines (BC & Saskatchewan) — served by the NGTL DOP API.

Foothills data lives in the same TC Energy Daily Operating Plan API as NGTL,
under areas FHBC and FHSK; this adapter filters to those and adds the
Foothills notices page and tolls documents.
"""
from __future__ import annotations

import html as htmllib
import re
from datetime import date

from ..dates import to_iso
from ..http import EbbClient
from ..models import NoticeRecord
from .base import FetchResult
from .common import pdf_links
from .ngtl import TCCE, NgtlAdapter


class FoothillsAdapter(NgtlAdapter):
    name = "foothills"
    # DOP labels Foothills areas FHBC/FHSK in /areas but FHZ8/FHZ9 in outages
    AREA_FILTER = {"FHBC", "FHSK", "FHZ8", "FHZ9"}
    # Foothills physical flows = NGTL border deliveries into its BC (Alberta
    # border) and Saskatchewan (Empress/McNeill) systems.
    CSR_FIELDS = [
        ("albertaBorderFlow", "ALBERTA_BORDER"),
        ("empressBorderFlow", "EMPRESS_BORDER"),
        ("mcneilBorderFlow", "MCNEIL_BORDER"),
    ]

    DATASETS = dict(NgtlAdapter.DATASETS, notices="fetch_notices")

    RATE_PAGES = {
        "tolls_bc": f"{TCCE}/2768.html",
        "tolls_sk": f"{TCCE}/2771.html",
        "tariff": f"{TCCE}/926.html",
    }

    # capability-forecast CSV columns are display names, not acronyms
    def _keep_area_forecast(self, column_name: str) -> bool:
        return "foothills" in column_name.lower() or "empress" in column_name.lower()

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse per-zone tolls from the newest Table of Effective Rates PDF."""
        from .. import ratesheets
        result = FetchResult()
        best = None
        for href, title in pdf_links(client.get(self.RATE_PAGES["tariff"]).text, base=TCCE):
            if "table of effective rates" not in title.lower():
                continue
            ym = re.search(r"(20\d{2})", title + " " + href)
            year = int(ym.group(1)) if ym else 0
            if best is None or year > best[0]:
                best = (year, href)
        if best is None:
            raise RuntimeError("no Table of Effective Rates link on the Foothills tariff page")
        content = client.get(best[1]).content
        texts = ratesheets.pdf_page_texts(content)
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_foothills(
            "\n".join(texts.values()), source_url=best[1]))
        return result

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Foothills informational notices page (HTML table, often empty)."""
        result = FetchResult()
        html = client.get(f"{TCCE}/925.html").text
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
        for row_html in rows:
            cells = [htmllib.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).replace("\xa0", " ").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html,
                                         re.DOTALL | re.IGNORECASE)]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            posted = to_iso(cells[0])
            if not re.match(r"\d{4}-\d{2}-\d{2}", posted):
                continue  # header/instruction rows, not notices
            result.notices.append(NoticeRecord(
                pipeline=self.name,
                notice_id=f"{cells[0]}:{cells[1][:60]}",
                category="other",
                subject=" | ".join(cells[1:3]),
                body_text=" | ".join(cells),
                posted_at=posted,
                url=f"{TCCE}/925.html",
            ))
        return result
