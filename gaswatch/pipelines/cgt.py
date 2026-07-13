"""PG&E California Gas Transmission (CGT) — Pipe Ranger.

Data comes from undocumented-but-open AEM servlets under
https://www.pge.com/bin/pipeline/. GETs need nothing special; POSTs are
rejected by the Akamai WAF unless X-Requested-With + Referer headers are sent.
Maintenance lives only in the server-rendered foghorn page HTML.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import time
from datetime import date, datetime, timedelta

import openpyxl
from lxml import html as lxml_html

from ..dates import month_day_range
from ..http import EbbClient
from ..models import CapacityRecord, FlowRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import cycle_slug, num as _num, row_cells

log = logging.getLogger("gaswatch.cgt")

BIN = "https://www.pge.com/bin/pipeline"
SITE = "https://www.pge.com/pipeline/en"
FOGHORN_URL = f"{SITE}/operating-data/current-pipeline-status/pipeline-maintenance/foghorn.html"
RATES_URL = f"{SITE}/products/rates.html"

XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{SITE}/operating-data/current-pipeline-status.html",
}


def _cycle_slug(raw: str) -> str:
    """Canonicalize PG&E's free-text cycle strings ('Evening Schedule',
    'Timely Unprocessed', 'EVENING'...) to the shared cycle vocabulary."""
    return cycle_slug(raw) or "current"


# one canonical location vocabulary: interactive-map keys (left) are mapped to
# the XLSX-archive slugs so live pulls and backfill land on the same series
LOCATION_SLUGS = {
    "redwood": "redwood_path",
    "onyx": "onyx_hill",
    "dagget": "daggett",
    "socalkrs": "kern_river_station",
    "caproduction": "ca_production",
    "topocknorth": "topock_north",
    "topocksouth": "topock_south",
    "freemontpeak_receipt": "freemont_peak_receipts",
    "freemontpeak_deliveries": "freemont_peak_delivery",
}


def _sd_kind(slug: str) -> tuple[str, str]:
    """Classify a supply/demand-archive metric row -> (kind, unit)."""
    if "temperature" in slug:
        return "temperature", "degF"
    if "inventory" in slug:
        return "inventory", "MMcf"
    if "imbalance" in slug:
        return "imbalance", "MMcf"
    if "storage" in slug:
        return "storage", "MMcf"
    if "demand" in slug or slug == "transmission_shrinkage":
        return "demand", "MMcf"
    if slug == "total_system_supply":
        return "supply", "MMcf"
    return "receipt", "MMcf"  # interconnect/production receipt rows


class CgtAdapter(PipelineAdapter):
    name = "cgt"
    DATASETS = {
        "capacity": "fetch_capacity",     # interactive map; XLSX archive for backfill
        "flows": "fetch_flows",           # scheduled volumes by path; supply/demand archive for backfill
        "ofo": "fetch_ofo",               # OFO/EFO event archive
        "maintenance": "fetch_maintenance",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values from the rate sheet PDF
    }
    HEAVY_DATASETS = ("rate_values",)

    # -- capacity ----------------------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        if start and end:
            return self._backfill_capacity(client, start, end)
        result = FetchResult()
        resp = client.get(f"{BIN}/interactivemap")
        result.raw_paths.append(client.archive(
            self.name, "capacity", f"interactivemap_{gas_day.isoformat()}.json", resp.content))
        data = resp.json().get("availableCapacityData") or {}
        for day_key in ("today", "tomorrow"):
            day_data = data.get(day_key) or {}
            day_iso = self._parse_map_date(day_data.get("date"), gas_day, day_key)
            cycle_raw = day_data.get("cycle") or day_data.get("cycle-status-key") or ""
            cycle = _cycle_slug(cycle_raw)
            for loc, value in day_data.items():
                if not isinstance(value, dict):
                    continue
                if "availableCapacity" in value or "scheduledVolume" in value \
                        or "physicalPipelineCapacity" in value:
                    result.capacity.append(self._map_record(loc, value, day_iso, cycle))
                else:  # nested (e.g. freemontpeak: {Receipt: {...}, Deliveries: {...}})
                    for sub, subval in value.items():
                        if isinstance(subval, dict):
                            result.capacity.append(
                                self._map_record(f"{loc}_{sub}", subval, day_iso, cycle))
        return result

    def _map_record(self, loc: str, value: dict, day_iso: str, cycle: str) -> CapacityRecord:
        slug = LOCATION_SLUGS.get(loc.lower(), loc.lower())
        return CapacityRecord(
            pipeline=self.name, gas_day=day_iso, cycle=cycle,
            location_type="path", location_id=slug, location_name=loc,
            operating_cap=_num(value.get("physicalPipelineCapacity")),
            scheduled_qty=_num(value.get("scheduledVolume")),
            available_cap=_num(value.get("availableCapacity")),
            unit="Dth",
        )

    @staticmethod
    def _parse_map_date(raw: str | None, gas_day: date, day_key: str) -> str:
        if raw:
            try:  # "July, 11 2026"
                return datetime.strptime(raw.strip(), "%B, %d %Y").date().isoformat()
            except ValueError:
                pass
        return (gas_day + timedelta(days=1 if day_key == "tomorrow" else 0)).isoformat()

    def _backfill_capacity(self, client: EbbClient, start: date, end: date) -> FetchResult:
        """Prior-day final capacity/scheduled via the interactive-map XLSX archive."""
        result = FetchResult()
        d = start
        while d <= end:
            body = {
                "startMapDay": base64.b64encode(d.strftime("%m/%d/%Y").encode()).decode(),
                "ts": str(int(time.time() * 1000)),
                "resourcePath": "/content/pipeline/language-masters/en",
            }
            try:
                resp = client.post(f"{BIN}/interactivemapexcel", data=body, headers=XHR_HEADERS)
            except Exception as exc:  # some archive days 500 server-side — skip, keep going
                log.warning("cgt: XLSX archive failed for %s (%s)", d, exc)
                d += timedelta(days=1)
                continue
            if not resp.content[:2] == b"PK":  # not an xlsx — day probably out of range
                log.warning("cgt: no XLSX archive for %s", d)
                d += timedelta(days=1)
                continue
            result.raw_paths.append(client.archive(
                self.name, "capacity", f"final_{d.isoformat()}.xlsx", resp.content))
            wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True)
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            next(rows, None)  # header
            for row in rows:
                if not row or not row[0]:
                    continue
                category, day_str, phys, sched, avail = (list(row) + [None] * 5)[:5]
                try:
                    day_iso = datetime.strptime(str(day_str), "%m/%d/%Y").date().isoformat()
                except (ValueError, TypeError):
                    continue
                result.capacity.append(CapacityRecord(
                    pipeline=self.name, gas_day=day_iso, cycle="final",
                    location_type="path", location_id=str(category).lower().replace(" ", "_"),
                    location_name=str(category),
                    operating_cap=_num(phys), scheduled_qty=_num(sched),
                    available_cap=_num(avail), unit="Dth",
                ))
            d += timedelta(days=1)
        return result

    # -- flows (scheduled volumes by path) -----------------------------------

    def fetch_flows(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        if start and end:
            return self._backfill_flows(client, start, end)
        result = FetchResult()
        resp = client.get(f"{BIN}/scheduledvolumes")
        result.raw_paths.append(client.archive(
            self.name, "flows", f"scheduledvolumes_{gas_day.isoformat()}.json", resp.content))
        rows = (resp.json().get("schd_values") or {}).get("v_schd_value") or []
        for row in rows:
            cycle = row.get("cycle", "")
            if "n/a" in cycle.lower():
                continue
            try:
                day_iso = datetime.strptime(row.get("gas_day", ""), "%m/%d/%y").date().isoformat()
            except ValueError:
                continue
            for key, value in row.items():
                if key in ("cycle", "gas_day"):
                    continue
                result.flows.append(FlowRecord(
                    pipeline=self.name, gas_day=day_iso, area=key,
                    flow=_num(value), unit="Dth", kind="scheduled",
                    extra={"cycle": cycle},
                ))
        return result

    def _backfill_flows(self, client: EbbClient, start: date, end: date) -> FetchResult:
        """Supply/demand archive: tab-delimited, dates as columns, metrics as rows."""
        result = FetchResult()
        resp = client.get(f"{BIN}/supplydemandarchive",
                          params={"start_date": start.isoformat(), "end_date": end.isoformat()})
        result.raw_paths.append(client.archive(
            self.name, "flows", f"supplydemand_{start.isoformat()}_{end.isoformat()}.txt",
            resp.content))
        lines = [ln.rstrip("\t") for ln in resp.text.splitlines() if ln.strip()]
        if not lines:
            return result
        dates = []
        for cell in lines[0].split("\t"):
            cell = cell.strip()
            if not cell:
                continue
            try:
                dates.append(datetime.strptime(cell, "%m/%d/%Y").date().isoformat())
            except ValueError:
                dates.append(None)
        for line in lines[1:]:
            cells = line.split("\t")
            metric = cells[0].strip()
            if not metric:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", metric.lower()).strip("_")
            kind, unit = _sd_kind(slug)
            for day_iso, cell in zip(dates, cells[1:]):
                if day_iso is None:
                    continue
                result.flows.append(FlowRecord(
                    pipeline=self.name, gas_day=day_iso, area=slug,
                    flow=_num(cell), unit=unit, kind=kind,
                    extra={"metric": metric},
                ))
        return result

    # -- OFO/EFO events -------------------------------------------------------

    def fetch_ofo(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = client.post(
            f"{BIN}/ofoefoarchive", data={"ofotype": "sortbydate"},
            headers={**XHR_HEADERS,
                     "Referer": f"{SITE}/operating-data/system-conditions/ofo-efo-archive.html"})
        result.raw_paths.append(client.archive(
            self.name, "ofo", f"ofoefo_{gas_day.isoformat()}.json", resp.content))
        for row in resp.json():
            day = row.get("gasDay", "")
            try:
                day_iso = datetime.strptime(day, "%m/%d/%Y").date().isoformat()
            except ValueError:
                day_iso = day
            result.notices.append(NoticeRecord(
                pipeline=self.name,
                notice_id=f"{day_iso}:{row.get('typeShortName', 'OFO')}",
                category="ofo",
                subject=f"{row.get('typeDesc', 'OFO')} stage {row.get('stage')} "
                        f"({row.get('reason', '')})",
                effective_start=day_iso,
                effective_end=day_iso,
                url=f"{SITE}/operating-data/system-conditions/ofo-efo-archive.html",
                extra={"stage": row.get("stage"), "tolerance_pct": row.get("tolerance"),
                       "noncompliance_charge": row.get("nonComplianceCharge"),
                       "reason": row.get("reason", "")},
            ))
        return result

    # -- maintenance (foghorn HTML) ---------------------------------------------

    def fetch_maintenance(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        resp = client.get(FOGHORN_URL)
        result.raw_paths.append(client.archive(
            self.name, "maintenance", f"foghorn_{gas_day.isoformat()}.html", resp.content))
        doc = lxml_html.fromstring(resp.content)
        for t_idx, table in enumerate(doc.xpath("//table")):
            headers = [th.text_content().strip() for th in table.xpath(".//th")]
            for row in table.xpath(".//tbody/tr") or table.xpath(".//tr"):
                cells = row_cells(row)
                if not cells or not any(cells):
                    continue
                row_headers = headers[-len(cells):] if len(headers) >= len(cells) else []
                fields = dict(zip(row_headers, cells))
                dates = fields.get("Dates", cells[0])
                notes = fields.get("Maintenance Notes", cells[-1])
                caps = {h: _num(v) for h, v in fields.items()
                        if h not in ("Dates", "Maintenance Notes")}
                digest = hashlib.sha1(f"{dates}|{notes}".encode()).hexdigest()[:12]
                start_iso, end_iso = month_day_range(dates, gas_day.year)
                result.notices.append(NoticeRecord(
                    pipeline=self.name,
                    notice_id=f"foghorn:{gas_day.year}:{digest}",
                    category="maintenance",
                    subject=f"{dates}: {notes[:150]}",
                    body_text=notes,
                    effective_start=start_iso,
                    effective_end=end_iso,
                    url=FOGHORN_URL,
                    extra={"table": t_idx, "capacities_mmcfd": caps, "dates_raw": dates},
                ))
        return result

    # -- rates -----------------------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        html_text = client.get(RATES_URL).text
        for m in re.finditer(r'href="([^"]+\.pdf[^"]*)"', html_text):
            href = m.group(1)
            if href.startswith("/"):
                href = "https://www.pge.com" + href
            fname = href.rsplit("/", 1)[-1].split(".pdf")[0]
            result.rate_docs.append(RateDoc(
                pipeline=self.name, doc_type="rates", title=fname[:200], url=href,
            ))
        # storage rates servlet — hash body for change detection
        resp = client.get(f"{BIN}/StorageRatesArchivalServlet")
        result.rate_docs.append(RateDoc(
            pipeline=self.name, doc_type="storage_rates", title="CGT storage rates (servlet)",
            url=f"{BIN}/StorageRatesArchivalServlet",
            content_hash=hashlib.sha1(resp.content).hexdigest(),
        ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the current-year backbone/storage rate sheet PDF into values."""
        from .. import ratesheets
        result = FetchResult()
        html_text = client.get(RATES_URL).text
        links: dict[int, str] = {}
        for m in re.finditer(r'href="([^"]*rates(\d{4})[^"]*\.pdf[^"]*)"', html_text):
            links[int(m.group(2))] = m.group(1)
        if not links:
            raise RuntimeError("no rates<year>.pdf link on the PG&E rates page")
        url = links[max(links)]
        if url.startswith("/"):
            url = "https://www.pge.com" + url
        content = client.get(url).content
        text = ratesheets.pdf_page_texts(content, [0], layout=True)[0]
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_cgt(text, source_url=url))
        return result
