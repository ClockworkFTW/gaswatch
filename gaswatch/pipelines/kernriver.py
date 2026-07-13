"""Kern River Gas Transmission (Berkshire Hathaway Energy) — services portal.

The OAC Retrieve/Download form is gated by reCAPTCHA v3 — an explicit anti-bot
control we do not work around. Instead, the portal home page serves a Daily
Operational Report on plain GET: per-constraint-point operating capacity,
scheduled quantities (Kern River + Mojave), and OAC for three gas days at the
latest posted cycle. Notices render inline and their bodies are direct-GET
PDFs. No history beyond the rolling three-day window.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from lxml import html as lxml_html

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import num as _num, pdf_body_text, probe_content_hash, row_cells

log = logging.getLogger("gaswatch.kernriver")

PORTAL = "https://services.kernrivergas.com/portal"
NOTICE_VIEWER = f"{PORTAL}/DesktopModules/Notices/Common/NoticeViewer.aspx"
TARIFF_PDF = f"{PORTAL}/DesktopModules/Tariff/Tariff/Controls/KernRiverTariff.pdf"

CYCLES = {"TIM": "timely", "EVE": "evening", "ID1": "id1", "ID2": "id2", "ID3": "id3"}

NOTICE_PAGES = {
    "critical": "Critical",
    "non_critical": "Non-Critical",
    "planned_outage": "Planned-Service-Outage",
}


class KernRiverAdapter(PipelineAdapter):
    name = "kernriver"
    DATASETS = {
        "capacity": "fetch_capacity",   # daily operational report (3-day window)
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values (13 MB tariff download)
    }
    HEAVY_DATASETS = ("rate_values",)

    # -- capacity (daily operational report) ------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        if start and end:
            log.warning("kernriver: no public history — OAC download is reCAPTCHA-gated; "
                        "only the rolling 3-day home report is collected")
        result = FetchResult()
        resp = client.get(f"{PORTAL}/")
        result.raw_paths.append(client.archive(
            self.name, "capacity", f"dailyreport_{gas_day.isoformat()}.html", resp.content))
        doc = lxml_html.fromstring(resp.content)
        table = doc.xpath('//table[contains(@id, "dgConstPoints")]')
        if not table:
            log.warning("kernriver: constraint-point table missing from home report")
            return result
        point_name, point_id = "", ""
        for tr in table[0].xpath(".//tr"):
            cells = row_cells(tr)
            if len(cells) == 1 and cells[0]:
                # section row: "Muddy Creek Compressor (054001)"
                m = re.match(r"(.+?)\s*\((\w+)\)\s*$", cells[0])
                point_name, point_id = (m.group(1), m.group(2)) if m else (cells[0], cells[0])
                continue
            if len(cells) < 6 or not re.match(r"\d{2}/\d{2}/\d{4}", cells[0]):
                continue
            day_iso = datetime.strptime(cells[0], "%m/%d/%Y").date().isoformat()
            kr_tsq, moj_tsq = _num(cells[3]), _num(cells[4])
            result.capacity.append(CapacityRecord(
                pipeline=self.name, gas_day=day_iso,
                cycle=CYCLES.get(cells[1].upper(), cells[1].lower()),
                location_type="segment", location_id=point_id, location_name=point_name,
                operating_cap=_num(cells[2]),
                scheduled_qty=(kr_tsq or 0) + (moj_tsq or 0) if kr_tsq is not None else None,
                available_cap=_num(cells[5]),
                unit="Dth",
                extra={"kern_river_tsq": kr_tsq, "mojave_tsq": moj_tsq},
            ))
        return result

    # -- notices ------------------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for category, page in NOTICE_PAGES.items():
            resp = client.get(f"{PORTAL}/Informational-Postings/Notices/{page}")
            result.raw_paths.append(client.archive(
                self.name, "notices", f"{page}_{gas_day.isoformat()}.html", resp.content))
            doc = lxml_html.fromstring(resp.content)
            for tbl in doc.xpath('//table[contains(@id, "GridView1")]'):
                heads = " ".join(th.text_content() for th in tbl.xpath(".//th"))
                if "Subject" not in heads:
                    continue
                for tr in tbl.xpath(".//tr[td]"):
                    cells = row_cells(tr)
                    # Type, Posted, Eff, End, Identifier, Subject, Response, Status, Prior
                    if len(cells) < 6 or not cells[4].isdigit():
                        continue
                    result.notices.append(NoticeRecord(
                        pipeline=self.name, notice_id=cells[4], category=category,
                        subject=cells[5][:300],
                        effective_start=to_iso(cells[2]), effective_end=to_iso(cells[3]),
                        posted_at=to_iso(cells[1]),
                        url=f"{NOTICE_VIEWER}?DocId={cells[4]}",
                        extra={"notice_type": cells[0],
                               "status": cells[7] if len(cells) > 7 else "",
                               "prior_notice_id": cells[8] if len(cells) > 8 else ""},
                    ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        """Notice bodies are PDFs served directly by NoticeViewer.aspx."""
        bodies: dict[str, str] = {}
        for n in notices:
            url = n.get("url") or ""
            if "NoticeViewer.aspx" not in url:
                continue
            try:
                resp = client.get(url)
                if not resp.content.startswith(b"%PDF"):
                    continue
                text = pdf_body_text(resp.content)
                if text:
                    bodies[n["notice_id"]] = text
            except Exception as exc:
                log.warning("kernriver: body fetch failed for %s: %s", n["notice_id"], exc)
        return bodies

    # -- rates ----------------------------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        # 13 MB tariff PDF — track version without downloading it
        result.rate_docs.append(RateDoc(
            pipeline=self.name, doc_type="tariff",
            title="Kern River entire tariff (PDF, incl. currently effective rates)",
            url=TARIFF_PDF,
            content_hash=probe_content_hash(client, TARIFF_PDF),
        ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the statement-of-rates sheets at the front of the tariff PDF."""
        from .. import ratesheets
        result = FetchResult()
        content = client.get(TARIFF_PDF).content
        texts = ratesheets.pdf_page_texts(content, list(range(40)))
        texts = {i: t for i, t in texts.items() if "STATEMENT OF RATES" in t}
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_kern(texts, source_url=TARIFF_PDF))
        return result
