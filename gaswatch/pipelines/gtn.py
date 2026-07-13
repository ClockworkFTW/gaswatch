"""Gas Transmission Northwest (TC Energy) — tcplus.com Ganesha InfoPost.

Everything is plain HTTP GET returning JSON. Same platform serves North Baja,
Tuscarora, Great Lakes; subclass and change BASE/IDPIPELINE to reuse.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import day_range, num as _num

log = logging.getLogger("gaswatch.gtn")

BASE = "https://www.tcplus.com/GTN"
IDPIPELINE = 3

# NoticeIndicator request codes; category is still derived from the response.
NOTICE_INDICATORS = {1: "critical", 2: "non_critical", 3: "planned_outage"}

CYCLE_SLUGS = {
    "Timely": "timely", "Evening": "evening",
    "Intraday 1": "id1", "Intraday 2": "id2", "Intraday 3": "id3",
}


def _fmt_day(d: date) -> str:
    return d.strftime("%m/%d/%y")


class GtnAdapter(PipelineAdapter):
    name = "gtn"
    DATASETS = {
        "capacity": "fetch_capacity",   # includes scheduled quantities (TSQ)
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values (6 MB tariff download)
    }
    HEAVY_DATASETS = ("rate_values",)

    # -- capacity (+ scheduled) ------------------------------------------------

    def _cycles_for_day(self, client: EbbClient, d: date) -> dict[str, str]:
        resp = client.get(
            f"{BASE}/CycleType/GetCycleTypeByGasDay/",
            params={"idPipeline": IDPIPELINE, "gasDay": _fmt_day(d)},
        )
        return {item["Key"]: item["Value"] for item in resp.json()}

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for d in day_range(gas_day, start, end):
            try:
                cycles = self._cycles_for_day(client, d)
            except Exception as exc:
                log.warning("gtn: no cycle list for %s (%s); assuming Timely", d, exc)
                cycles = {"1": "Timely"}
            for key, cycle_name in cycles.items():
                resp = client.get(
                    f"{BASE}/OperationalCapacity/Generate",
                    params={"filter.GasDay": _fmt_day(d), "filter.CycleType": key},
                )
                payload = resp.json().get("data") or {}
                content = payload.get("Content") or []
                if not content:
                    continue
                cycle = CYCLE_SLUGS.get(payload.get("Cycle", cycle_name), cycle_name.lower())
                result.raw_paths.append(client.archive(
                    self.name, "capacity", f"{d.isoformat()}_{cycle}.json", resp.content))
                for row in content:
                    loc_type = ("segment" if "Segment" in (row.get("LocationPurposeDescription") or "")
                                else "point")
                    result.capacity.append(CapacityRecord(
                        pipeline=self.name,
                        gas_day=d.isoformat(),
                        cycle=cycle,
                        location_type=loc_type,
                        location_id=str(row.get("LocationID", "")),
                        location_name=row.get("LocationName", ""),
                        design_cap=_num(row.get("DesignCapacity")),
                        operating_cap=_num(row.get("OperatingCapacity")),
                        scheduled_qty=_num(row.get("TotalScheduledQuantity")),
                        available_cap=_num(row.get("OperationallyAvailableCapacity")),
                        unit="Dth",  # posting says MMBtu; 1 MMBtu = 1 Dth — one label everywhere
                        flow_direction=row.get("FlowIndicatorDescription", ""),
                        extra={"loc_qti": row.get("LocQti", ""), "it": row.get("IT", ""),
                               "all_qty_avail": row.get("AllQtyAvailable", "")},
                    ))
        return result

    # -- notices ---------------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        for indicator in NOTICE_INDICATORS:
            page = 1
            while True:
                # NB: never send an empty filter.SelectedTypeIds= (server 500s).
                resp = client.get(
                    f"{BASE}/Notice/Retrieve",
                    params={
                        "filter.SelectedIndicator": indicator,
                        "filter.SelectedStatus": "", "filter.EffDate": "", "filter.EndDate": "",
                        "page": page, "sort": "PostingDate", "sort_direction": "descending",
                    },
                )
                payload = resp.json()
                rows = payload.get("data") or []
                for row in rows:
                    notice_id = str(row.get("NoticeId") or row.get("Id"))
                    if notice_id in seen:
                        continue
                    seen.add(notice_id)
                    indicator_name = (row.get("NoticeIndicator") or "").strip()
                    category = {
                        "Critical": "critical", "Non-Critical": "non_critical",
                        "Planned Service Outage": "planned_outage",
                    }.get(indicator_name, indicator_name.lower().replace(" ", "_") or "other")
                    result.notices.append(NoticeRecord(
                        pipeline=self.name,
                        notice_id=notice_id,
                        category=category,
                        subject=(row.get("Subject") or "").strip(),
                        body_text=row.get("Text") or "",
                        effective_start=to_iso(f"{row.get('EffDate', '')} {row.get('EffTime', '')}".strip()),
                        effective_end=to_iso(f"{row.get('EndDate', '')} {row.get('EndTime', '')}".strip()),
                        posted_at=to_iso(f"{row.get('PostingDate', '')} {row.get('PostingTime', '')}".strip()),
                        url=f"{BASE}/Notice",
                        extra={"status": row.get("NoticeStatus", ""),
                               "type": row.get("NoticeType", ""),
                               "prior_notice_id": row.get("PriorNoticeID")},
                    ))
                total = payload.get("total") or 0
                page_size = payload.get("page_size") or len(rows) or 1
                if page * page_size >= total or not rows:
                    break
                page += 1
        return result

    # -- rates / tariff --------------------------------------------------------

    RATE_PAGES = {
        "rates": f"{BASE}/Tariff/CurrEffRates",
        "tariff": f"{BASE}/Tariff/EntireTariff",
    }

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for doc_type, url in self.RATE_PAGES.items():
            resp = client.get(url)
            html = resp.text
            # Tariff PDFs sit behind per-version 40-char hash links; the hash is
            # the version identity, so no download is needed for change detection.
            for match in re.finditer(
                    r'SharedFolder/DisplayFile/([0-9a-f]{40})\?downloadType=(\w+)', html):
                file_hash, dl_type = match.groups()
                result.rate_docs.append(RateDoc(
                    pipeline=self.name,
                    doc_type=doc_type,
                    title=f"GTN {doc_type} ({dl_type})",
                    url=f"{BASE}/SharedFolder/DisplayFile/{file_hash}?downloadType={dl_type}",
                    content_hash=file_hash,
                ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the Part 4 statement-of-rates pages of the tariff PDF."""
        from .. import ratesheets
        result = FetchResult()
        html = client.get(self.RATE_PAGES["rates"]).text
        m = re.search(r'SharedFolder/DisplayFile/([0-9a-f]{40})\?downloadType=Tariff', html)
        if not m:
            raise RuntimeError("no tariff PDF link on the GTN current-rates page")
        url = f"{BASE}/SharedFolder/DisplayFile/{m.group(1)}?downloadType=Tariff"
        content = client.get(url).content
        texts = ratesheets.pdf_page_texts(content, list(range(30)))
        texts = {i: t for i, t in texts.items() if "Statement of Rates" in t}
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_gtn(texts, source_url=url))
        return result
