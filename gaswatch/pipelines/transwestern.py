"""Transwestern Pipeline (Energy Transfer) — Messenger "ipost" platform.

Plain HTTP GET everywhere; every report exports CSV with f=csv&extension=csv.
Gotchas (verified): the OA CSV carries no gas-day/cycle columns (stamp them
from the request); unknown params fall back silently instead of erroring;
notice-list CSVs cap at ~1,000 rows regardless of max=ALL.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date

from lxml import html as lxml_html

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import clean_body, day_range, num as _num

log = logging.getLogger("gaswatch.transwestern")

BASE = "https://twtransfer.energytransfer.com/ipost"
ASSET = "TW"

# cycle dropdown shows Timely/Evening/Intraday 1/Intraday 2; only 1=Timely is
# verified in the wild — invalid values fall back silently, so stick to it for
# history and treat the rest as best-effort current-day views
CYCLES = {"1": "timely", "2": "evening", "3": "id1", "4": "id2"}

NOTICE_PAGES = {
    "critical": "critical",
    "non_critical": "non-critical",
    "planned_outage": "planned-service-outage",
}


def _fmt_day(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class TranswesternAdapter(PipelineAdapter):
    name = "transwestern"
    DATASETS = {
        "capacity": "fetch_capacity",   # OA posting; TSQ column = scheduled
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values from the rate matrix PDF
    }
    HEAVY_DATASETS = ("rate_values",)
    BACKFILL_DATASETS = ("capacity",)  # capacity CSV back to ~2015

    # -- capacity ------------------------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        cycles = list(CYCLES.items())
        if start and end:
            cycles = [("1", "timely")]  # only verified cycle for history
        for d in day_range(gas_day, start, end):
            for cycle_val, cycle in cycles:
                resp = client.get(
                    f"{BASE}/capacity/operationally-available",
                    params={"f": "csv", "extension": "csv", "asset": ASSET,
                            "gasDay": _fmt_day(d), "cycle": cycle_val,
                            "searchType": "NOM", "searchString": "",
                            "locType": "ALL", "locZone": "ALL"},
                )
                rows = list(csv.DictReader(io.StringIO(resp.text)))
                if not rows:
                    continue
                if cycle_val == "1" or not (start and end):
                    result.raw_paths.append(client.archive(
                        self.name, "capacity", f"{d.isoformat()}_{cycle}.csv", resp.content))
                for row in rows:
                    loc = (row.get("Loc") or "").strip()
                    if not loc:
                        continue
                    result.capacity.append(CapacityRecord(
                        pipeline=self.name,
                        gas_day=d.isoformat(),  # CSV has no gas-day column
                        cycle=cycle,
                        location_type="point",
                        location_id=loc,
                        location_name=(row.get("Loc Name") or "").strip(),
                        design_cap=_num(row.get("DC")),
                        operating_cap=_num(row.get("OPC")),
                        scheduled_qty=_num(row.get("TSQ")),
                        available_cap=_num(row.get("OAC")),
                        unit="Dth",
                        flow_direction=(row.get("Flow Ind") or "").strip(),
                        extra={"zone": (row.get("Loc Zn") or "").strip(),
                               "loc_qti": (row.get("Loc/QTI") or "").strip(),
                               "it": (row.get("IT") or "").strip(),
                               "all_qty_avail": (row.get("All Qty Avail") or "").strip()},
                    ))
        return result

    # -- notices ---------------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for category, page in NOTICE_PAGES.items():
            resp = client.get(
                f"{BASE}/notice/{page}",
                params={"asset": ASSET, "f": "csv", "extension": "csv", "max": "ALL"},
            )
            result.raw_paths.append(client.archive(
                self.name, "notices", f"{page}_{gas_day.isoformat()}.csv", resp.content))
            for row in csv.DictReader(io.StringIO(resp.text)):
                notice_id = (row.get("Notice ID") or "").strip()
                if not notice_id:
                    continue
                result.notices.append(NoticeRecord(
                    pipeline=self.name, notice_id=notice_id, category=category,
                    subject=(row.get("Subject") or "").strip(),
                    effective_start=to_iso(row.get("Notice Eff Date/Time")),
                    effective_end=to_iso(row.get("Notice End Date/Time")),
                    posted_at=to_iso(row.get("Posted Date/Time")),
                    url=f"{BASE}/notice/show/{notice_id}?asset={ASSET}",
                    extra={"notice_type": (row.get("Notice Type") or "").strip()},
                ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        bodies: dict[str, str] = {}
        for n in notices:
            url = n.get("url") or ""
            if "/notice/show/" not in url:
                continue
            resp = client.get(url)
            doc = lxml_html.fromstring(resp.text)
            for bad in doc.xpath("//script | //style | //nav | //header | //footer"):
                bad.getparent().remove(bad)
            text = re.sub(r"[ \t]+", " ", doc.text_content())
            # body follows the metadata block; anchor on the Subject line
            idx = text.find("Subject")
            body = clean_body(text[idx:] if idx > -1 else text)
            if body:
                bodies[n["notice_id"]] = body
        return bodies

    # -- rates -------------------------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = client.get(f"{BASE}/rates/index", params={"asset": ASSET})
        doc = lxml_html.fromstring(resp.text)
        for a in doc.xpath("//a[@href]"):
            href = a.get("href")
            if "posted-documents" not in href:
                continue
            title = re.sub(r"\s+", " ", a.text_content()).strip()
            if href.startswith("/"):
                href = "https://twtransfer.energytransfer.com" + href
            result.rate_docs.append(RateDoc(
                pipeline=self.name, doc_type="rates", title=title[:200] or href[-60:],
                url=href,
            ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the newest effective maximum-rate matrix (zone-to-zone
        reservation/commodity/fuel) out of its posted PDF."""
        from .. import ratesheets
        from ..rates import parse_effective
        result = FetchResult()
        docs = self.fetch_rates(client, gas_day).rate_docs
        best = None
        for doc in docs:
            if "rate matrix" not in doc.title.lower():
                continue
            eff, _key = parse_effective(doc.title)
            if eff and (best is None or eff > best[0]):
                best = (eff, doc)
        if best is None:
            raise RuntimeError("no dated rate-matrix posting found on the TW rates page")
        viewer = client.get(best[1].url, params={"asset": ASSET}).text
        m = re.search(r'\.src\s*=\s*"([^"]+\.pdf)', viewer)
        if not m:
            raise RuntimeError(f"no embedded PDF in TW viewer page {best[1].url}")
        pdf_url = m.group(1)
        content = client.get(pdf_url).content
        texts = ratesheets.pdf_page_texts(content, layout=True)
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_tw(texts, source_url=pdf_url))
        return result
