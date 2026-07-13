"""Northwest Pipeline (Williams) — NWP_Portal informational postings.

Plain HTTP GET, server-rendered HTML (Struts .action endpoints), no auth/JS.
The OAC report serves one gas day per request at the latest posted cycle and
carries history back to January 1998. Scheduled quantities are the Total
Scheduled Qty column of the OAC posting (no separate report). Notice bodies
come from a plain-text NAESB download endpoint.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from lxml import html as lxml_html

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import (clean_body, cycle_slug, day_range, num as _num,
                     probe_content_hash, row_cells)

log = logging.getLogger("gaswatch.nwp")

BASE = "https://www.northwest.williams.com/NWP_Portal"

NOTICE_TYPES = {
    "critical": "Critical Notes",
    "non_critical": "Non Critical Notes",
    "planned_outage": "Planned Service Outage",
}


def _cycle_slug(raw: str) -> str:
    s = (raw or "").lower()
    return cycle_slug(s) or re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "current"


class NwpAdapter(PipelineAdapter):
    name = "nwp"
    DATASETS = {
        "capacity": "fetch_capacity",   # OAC posting; TSQ column = scheduled
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values from the tariff PDF
    }
    HEAVY_DATASETS = ("rate_values",)
    BACKFILL_DATASETS = ("capacity",)  # OAC history to 1998 (slow, ~1 req/s)

    # -- capacity -------------------------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for d in day_range(gas_day, start, end):
            day_str = d.strftime("%m-%d-%Y")
            try:
                resp = client.get(
                    f"{BASE}/CapacityResultsScrollable.action",
                    params={"StartGasFlowDate": day_str, "EndGasFlowDate": day_str,
                            "RptType": "OA", "RptPart": "SCROLL"},
                )
            except Exception as exc:
                log.warning("nwp: capacity fetch failed for %s (%s)", d, exc)
                continue
            doc = lxml_html.fromstring(resp.content)
            m = re.search(r"Cycle:\s*([^\n\r<]{1,60})", doc.text_content())
            cycle = _cycle_slug(m.group(1).strip() if m else "")
            n = 0
            for tr in doc.xpath("//table//tr"):
                cells = row_cells(tr)
                # LocProp | LocName | Loc | LocPurpDesc | FlowIndDesc | Loc/QTI |
                # DC | OC | TSQ | OAC | IT
                if len(cells) < 10 or not cells[2].isdigit():
                    continue
                result.capacity.append(CapacityRecord(
                    pipeline=self.name, gas_day=d.isoformat(), cycle=cycle,
                    location_type="point", location_id=cells[2],
                    location_name=cells[1],
                    design_cap=_num(cells[6]), operating_cap=_num(cells[7]),
                    scheduled_qty=_num(cells[8]), available_cap=_num(cells[9]),
                    unit="Dth", flow_direction=cells[4],
                    extra={"loc_prop": cells[0], "loc_purpose": cells[3],
                           "loc_qti": cells[5],
                           "it": cells[10] if len(cells) > 10 else ""},
                ))
                n += 1
            if n:
                result.raw_paths.append(client.archive(
                    self.name, "capacity", f"{d.isoformat()}_{cycle}.html", resp.content))
            else:
                log.warning("nwp: no capacity rows for %s", d)
        return result

    # -- notices ---------------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for category, type_name in NOTICE_TYPES.items():
            resp = client.get(
                f"{BASE}/northwest_notice_list.action",
                params={"NoticeType": type_name, "RptPart": "SCROLL"},
            )
            result.raw_paths.append(client.archive(
                self.name, "notices",
                f"{category}_{gas_day.isoformat()}.html", resp.content))
            doc = lxml_html.fromstring(resp.content)
            seen: set[str] = set()
            for a in doc.xpath('//a[contains(@href, "notice_num=")]'):
                m = re.search(r"notice_num=(\d+)", a.get("href", ""))
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                notice_num = m.group(1)
                tr = a.getparent()
                while tr is not None and tr.tag != "tr":
                    tr = tr.getparent()
                if tr is None:
                    continue
                cells = row_cells(tr)
                # Type | Posted | Eff | End | DisplayID | ? | Subject | RspD/T | Download
                if len(cells) < 7:
                    continue
                result.notices.append(NoticeRecord(
                    pipeline=self.name, notice_id=notice_num, category=category,
                    subject=cells[6][:300] or cells[0],
                    effective_start=to_iso(cells[2]), effective_end=to_iso(cells[3]),
                    posted_at=to_iso(cells[1]),
                    url=f"{BASE}/NorthwestDownload?notice_num={notice_num}",
                    extra={"notice_type": cells[0], "display_id": cells[4]},
                ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        """Bodies come from the plain-text NAESB download endpoint."""
        import html as htmllib
        bodies: dict[str, str] = {}
        for n in notices:
            url = n.get("url") or ""
            if "NorthwestDownload" not in url:
                continue
            resp = client.get(url)
            text = htmllib.unescape(resp.text)
            text = re.sub(r"<br\s*/?>", "\n", text)
            text = re.sub(r"<[^>]+>", "", text)
            idx = text.find("Notice Text")
            body = clean_body(text[idx:] if idx > -1 else text)
            if body:
                bodies[n["notice_id"]] = body
        return bodies

    # -- rates ------------------------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        url = f"{BASE}/file_download?hfFileURL=Files/Northwest/tariff/tariff.pdf"
        result.rate_docs.append(RateDoc(
            pipeline=self.name, doc_type="tariff",
            title="Northwest Pipeline entire tariff (PDF, incl. rates)",
            url=url,
            content_hash=probe_content_hash(client, url),
        ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the statement-of-rates sheets at the front of the tariff PDF."""
        from .. import ratesheets
        result = FetchResult()
        url = f"{BASE}/file_download?hfFileURL=Files/Northwest/tariff/tariff.pdf"
        content = client.get(url).content
        texts = ratesheets.pdf_page_texts(content, list(range(30)))
        texts = {i: t for i, t in texts.items() if "STATEMENT OF RATES" in t}
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_nwp(texts, source_url=url))
        return result
