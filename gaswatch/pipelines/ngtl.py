"""NOVA Gas Transmission (NGTL) — TC Energy Daily Operating Plan API.

The my.tccustomerexpress.com SPA is backed by an open AWS API Gateway. The
gateway base URL is embedded in the SPA JS bundle and can rotate on redeploys,
so the adapter rediscovers it on failure. The same API also carries the
Foothills areas (FHBC/FHSK) — see foothills.py.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime, timedelta

from ..http import EbbClient
from ..models import CapacityRecord, FlowRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import num as _num, pdf_links

log = logging.getLogger("gaswatch.ngtl")

DEFAULT_API_BASE = "https://f51561ras5.execute-api.us-west-2.amazonaws.com/production"
SPA_URL = "https://my.tccustomerexpress.com/"
TCCE = "https://www.tccustomerexpress.com"

# Units: DOP flows/outage capabilities are 10^3 m3/d; capability forecast CSV is 10^6 m3/d.
UNIT_FLOW = "e3m3/d"
UNIT_CAPABILITY_FORECAST = "e6m3/d"


def _outage_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


class NgtlAdapter(PipelineAdapter):
    name = "ngtl"
    DATASETS = {
        "flows": "fetch_flows",         # daily flow vs capability + live system report
        "capacity": "fetch_capacity",   # monthly base-capability forecast by area
        "outages": "fetch_outages",     # maintenance/outage events -> notices
        "rates": "fetch_rates",
        "cer": "fetch_cer",             # official CER key-point history, 2006-present
        "rate_values": "fetch_rate_values",  # parsed headline tolls
    }
    HEAVY_DATASETS = ("rate_values",)
    BACKFILL_DATASETS = ("flows",)  # DOP API by gas day (inherited by Foothills)

    # CER open-data daily throughput/capacity (quarterly-updated, Open
    # Government Licence – Canada). One-time/occasional import.
    CER_CSV = ("https://www.cer-rec.gc.ca/open/energy/throughput-capacity/"
               "ngtl-throughput-and-capacity.csv")

    def fetch_cer(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = client.get(self.CER_CSV)
        result.raw_paths.append(client.archive(
            self.name, "cer", f"cer_{gas_day.isoformat()}.csv", resp.content))
        d0 = start.isoformat() if start else "0000"
        d1 = end.isoformat() if end else "9999"
        reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
        for row in reader:
            day = (row.get("Date") or "").strip()
            if not (d0 <= day <= d1):
                continue
            key_point = (row.get("Key Point") or "").strip()
            if not key_point:
                continue
            result.flows.append(FlowRecord(
                pipeline=self.name, gas_day=day,
                area=f"CER:{key_point}",
                flow=_num(row.get("Throughput (1000 m3/d)")),
                capability=_num(row.get("Capacity (1000 m3/d)")),
                unit=UNIT_FLOW, kind="cer_throughput",
                extra={"direction": (row.get("Direction Of Flow") or "").strip(),
                       "throughput_gj": _num(row.get("Throughput (GJ/d)"))},
            ))
        return result

    # Areas this adapter reports on (None = all). Foothills subclass narrows it.
    AREA_FILTER: set[str] | None = None
    # Current-system-report fields captured as snapshot flow records.
    CSR_FIELDS: list[tuple[str, str]] = [
        ("totalReceipts", "SYSTEM_RECEIPTS"), ("totalDeliveries", "SYSTEM_DELIVERIES"),
        ("currentLinepack", "LINEPACK"), ("empressBorderFlow", "EMPRESS_BORDER"),
        ("mcneilBorderFlow", "MCNEIL_BORDER"), ("albertaBorderFlow", "ALBERTA_BORDER"),
    ]
    _api_base: str | None = None

    # -- API base discovery ----------------------------------------------------

    @classmethod
    def _discover_api_base(cls, client: EbbClient) -> str:
        html = client.get(SPA_URL).text
        js_paths = re.findall(r'src="(/static/js/[^"]+\.js)"', html)
        for path in js_paths:
            js = client.get(SPA_URL.rstrip("/") + path).text
            m = re.search(r'https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com/\w+', js)
            if m:
                log.info("ngtl: discovered API base %s", m.group(0))
                return m.group(0)
        raise RuntimeError("could not discover NGTL API base from SPA bundle")

    def _get(self, client: EbbClient, path: str, params: dict | None = None):
        base = NgtlAdapter._api_base or DEFAULT_API_BASE
        try:
            return client.get(base + path, params=params)
        except Exception:
            fresh = self._discover_api_base(client)
            if fresh == base:
                raise
            NgtlAdapter._api_base = fresh
            return client.get(fresh + path, params=params)

    def _areas(self, client: EbbClient) -> dict[int, str]:
        data = self._get(client, "/areas").json()["data"]
        return {a["id"]: a["acronym"] for a in data}

    def _keep_area(self, acronym: str) -> bool:
        return self.AREA_FILTER is None or acronym in self.AREA_FILTER

    # -- flows -------------------------------------------------------------

    def fetch_flows(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        d0 = start or gas_day - timedelta(days=3)
        d1 = end or gas_day
        areas = self._areas(client)
        resp = self._get(client, "/chart",
                         params={"start": d0.isoformat(), "end": d1.isoformat()})
        result.raw_paths.append(client.archive(
            self.name, "flows", f"chart_{d0.isoformat()}_{d1.isoformat()}.json", resp.content))
        for row in resp.json().get("data", []):
            acronym = areas.get(row.get("areaId"), str(row.get("areaId")))
            if not self._keep_area(acronym):
                continue
            result.flows.append(FlowRecord(
                pipeline=self.name,
                gas_day=row.get("gasDay", ""),
                area=acronym,
                flow=row.get("flow"),
                capability=row.get("capabilityVolume"),
                unit=UNIT_FLOW,
                kind="actual",
                extra={"historical_flow": row.get("historicalFlow"),
                       "historical_gas_day": row.get("historicalGasDay")},
            ))
        # Live current-system-report snapshot.
        if self.CSR_FIELDS:
            csr = self._get(client, "/csr").json().get("data") or []
            if csr:
                snap = csr[0]
                day = gas_day.isoformat()
                for field, area in self.CSR_FIELDS:
                    result.flows.append(FlowRecord(
                        pipeline=self.name, gas_day=day, area=area,
                        flow=snap.get(field), unit=UNIT_FLOW, kind="snapshot",
                        extra={"last_updated": snap.get("lastUpdated", "")},
                    ))
        return result

    # -- capability forecast -----------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = self._get(client, "/csv/capabilities")
        result.raw_paths.append(client.archive(
            self.name, "capacity", f"capabilities_{gas_day.isoformat()}.csv", resp.content))
        rows = list(csv.reader(io.StringIO(resp.text)))
        if not rows:
            return result
        header = [h.strip() for h in rows[0]]
        # first column is the month; trailing metadata columns have no data rows
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            try:
                month = datetime.strptime(row[0].strip(), "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
            for idx, area in enumerate(header[1:], start=1):
                if idx >= len(row) or not area or area.startswith(("Last updated", "Units")):
                    continue
                if not self._keep_area_forecast(area):
                    continue
                value = _num(row[idx])
                if value is None:
                    continue
                result.capacity.append(CapacityRecord(
                    pipeline=self.name, gas_day=month, cycle="monthly_forecast",
                    location_type="area", location_id=area, location_name=area,
                    operating_cap=value, unit=UNIT_CAPABILITY_FORECAST,
                ))
        return result

    def _keep_area_forecast(self, column_name: str) -> bool:
        return True

    # -- outages / maintenance ----------------------------------------------

    def fetch_outages(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = self._get(client, "/csv/outages/")
        result.raw_paths.append(client.archive(
            self.name, "outages", f"outages_{gas_day.isoformat()}.csv", resp.content))
        reader = csv.DictReader(io.StringIO(resp.text))
        foothills_areas = {"FHBC", "FHSK", "FHZ8", "FHZ9"}
        for row in reader:
            area = (row.get("Table") or "").strip()
            if not self._keep_area(area):
                continue
            # Foothills-zone outages are also stored by the foothills adapter;
            # tag them here so consumers can dedupe cross-pipeline counts
            shared = self.AREA_FILTER is None and area in foothills_areas
            result.notices.append(NoticeRecord(
                pipeline=self.name,
                notice_id=str(row.get("Outage Id", "")).strip(),
                category="maintenance",
                subject=(row.get("Description") or "").strip(),
                body_text=(row.get("Type of Restriction") or "").strip(),
                effective_start=_outage_date(row.get("Start", "")),
                effective_end=_outage_date(row.get("End", "")),
                url=SPA_URL,
                extra={
                    "area": area,
                    "shared_with_foothills": shared,
                    "capability": _num(row.get("Capability")),
                    "typical_flow": (row.get("Typical Flow") or "").strip(),
                    "capability_unit": UNIT_FLOW,
                    "other_restricted_segments": (row.get("Other Restricted Segments") or "").strip(),
                },
            ))
        return result

    # -- rates / tolls -------------------------------------------------------

    RATE_PAGES = {
        "tolls": f"{TCCE}/854.html",
        "tariff": f"{TCCE}/2766.html",
    }

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for doc_type, page_url in self.RATE_PAGES.items():
            for href, title in pdf_links(client.get(page_url).text, base=TCCE):
                title = title or href.rsplit("/", 1)[-1]
                result.rate_docs.append(RateDoc(
                    pipeline=self.name, doc_type=doc_type, title=title[:200], url=href,
                ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse headline tolls from the newest Table of Rates, Tolls and Charges."""
        from .. import ratesheets
        from ..rates import parse_effective
        result = FetchResult()
        best = None
        for href, title in pdf_links(client.get(self.RATE_PAGES["tolls"]).text, base=TCCE):
            if "table of rates" not in title.lower():
                continue
            eff, _key = parse_effective(title)
            if eff and (best is None or eff > best[0]):
                best = (eff, href)
        if best is None:
            raise RuntimeError("no dated Table of Rates posting on the NGTL tolls page")
        content = client.get(best[1]).content
        texts = ratesheets.pdf_page_texts(content, layout=True)
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_ngtl(
            "\n".join(texts.values()), source_url=best[1]))
        return result
