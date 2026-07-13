"""SoCal Gas ENVOY (Sempra) — envoy2.sempra.com public postings.

Every report has a CSV export via the `submit*SaveAs` pattern
(`Class=...CSVExportType`). Most reports accept plain GET, but the capacity
export only honors its gas-day/cycle filter when the form is POSTed; monthly
archive workbooks reach back to 2000 for deep history. Notices ledger rows are
JSON-ish objects embedded in the HTML (mixed quote styles — parsed by regex,
not json.loads).
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, timedelta

from lxml import html as lxml_html

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, FlowRecord, NoticeRecord
from .base import FetchResult, PipelineAdapter
from .common import day_range, num as _num

log = logging.getLogger("gaswatch.socal")

BASE = "https://envoy2.sempra.com/Public"
CSV_CLASS = "com.sempra.krypton.common.saveas.constants.CSVExportType"

CYCLES = {"1": "timely", "2": "evening", "3": "id1", "4": "id2", "5": "id3", "6": "id4"}

NOTICE_FOLDERS = {1: "critical", 2: "non_critical", 3: "tariff"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _fmt(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class SocalAdapter(PipelineAdapter):
    name = "socal"
    DATASETS = {
        "capacity": "fetch_capacity",     # receipt-point capacity utilization
        "flows": "fetch_flows",           # daily operations: receipts/sendout
        "maintenance": "fetch_maintenance",
        "notices": "fetch_notices",       # critical (incl. OFO events), non-critical, rates
        "capacity_archive": "fetch_capacity_archive",  # monthly workbooks to 2000
        "rates": "fetch_rates",           # CPUC tariff-book index (change tracking)
        "rate_values": "fetch_rate_values",  # parsed G-BTS backbone rates
    }
    HEAVY_DATASETS = ("rate_values",)
    BACKFILL_DATASETS = ("capacity_archive",)  # monthly workbooks back to 2000

    # SoCal tariffs are CPUC-filed, published through the tariff-book SPA whose
    # backing JSON API serves both the schedule index and the current PDFs.
    TARIFF_API = ("https://scg-uofa-api-prd-hzczb4hja0g6dcfv.a03.azurefd.net/"
                  "scg-uofa-wpubtm-prd")
    TARIFF_VIEW = "https://tariffsprd.socalgas.com/view/tariff/"
    # gas transmission + storage schedules worth tracking (core retail excluded)
    TARIFF_IDS = {"G-BTS", "G-BSS", "G-AUC", "G-LTS", "G-TBS", "G-SMT"}

    ARCHIVE_URL = "https://envoy2.sempra.com/Public/ViewExternal.download"

    # -- rates / tariff ----------------------------------------------------------

    def _tariff_index(self, client: EbbClient) -> list[dict]:
        resp = client.get(f"{self.TARIFF_API}/tariffs",
                          params={"utilId": "SCG", "bookId": "GAS",
                                  "sectId": "G-SCHEDS"})
        return resp.json()

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Track the transmission/storage schedules of the CPUC gas tariff book.

        Sheet counts move whenever a revised sheet is filed, so they serve as
        the change-detection hash."""
        from ..models import RateDoc
        result = FetchResult()
        for item in self._tariff_index(client):
            if item.get("TARF_ID") not in self.TARIFF_IDS:
                continue
            key = item.get("TARF_KEY")
            result.rate_docs.append(RateDoc(
                pipeline=self.name, doc_type="rates",
                title=f"Schedule {item['TARF_ID']} — {item.get('TARF_NAME', '')}"[:200],
                url=f"{self.TARIFF_VIEW}?utilId=SCG&bookId=GAS&tarfKey={key}",
                content_hash=f"{item.get('EFFECT_SHET_COUNT')}:"
                             f"{item.get('CURRENT_SHET_COUNT')}",
                extra={"tarf_key": key},
            ))
        if not result.rate_docs:
            raise RuntimeError("SoCal tariff API returned no tracked schedules")
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse backbone transportation rates from the current G-BTS PDF."""
        from .. import ratesheets
        result = FetchResult()
        key = next((i["TARF_KEY"] for i in self._tariff_index(client)
                    if i.get("TARF_ID") == "G-BTS"), None)
        if key is None:
            raise RuntimeError("G-BTS not found in the SoCal tariff index")
        url = f"{self.TARIFF_API}/tariff/?utilId=SCG&bookId=GAS&tarfKey={key}"
        content = client.get(url).content
        texts = ratesheets.pdf_page_texts(content)
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_socal(
            texts, source_url=f"{self.TARIFF_VIEW}?utilId=SCG&bookId=GAS&tarfKey={key}"))
        return result

    def fetch_capacity_archive(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Monthly capacity archive workbooks (xlsx from ~2013, xls before).

        Imports Timely-cycle rows only, matching the backfill convention of
        the other pipelines. Use e.g. --start 2000-01-01 --end 2026-05-31.
        """
        result = FetchResult()
        d0 = start or gas_day.replace(day=1)
        d1 = end or gas_day
        y, m = d0.year, d0.month
        while (y, m) <= (d1.year, d1.month):
            rows = self._archive_month(client, y, m)
            result.capacity.extend(rows)
            if not rows:
                log.warning("socal: no capacity archive for %d-%02d", y, m)
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return result

    def _archive_month(self, client: EbbClient, year: int, month: int) -> list[CapacityRecord]:
        content = None
        for ext in ("xlsx", "xls"):
            try:
                resp = client.get(self.ARCHIVE_URL,
                                  params={"file": f"/capacity/archive/capacity_{month}_{year}.{ext}"})
            except Exception:
                continue
            body = resp.content
            if body[:2] in (b"PK", b"\xd0\xcf") or body[:8].lstrip()[:4] == b"Flow":
                content = body
                break
        if content is None:
            return []
        rows: list[list] = []
        if content[:8].lstrip()[:4] == b"Flow":
            # old-era "xls" files are really delimited text
            text = content.decode("latin-1")
            delim = "\t" if "\t" in text[:500] else ","
            rows = [line for line in csv.reader(io.StringIO(text), delimiter=delim)]
        elif content[:2] == b"PK":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
            ws = wb.active
            # workbooks from ~2016-2023 carry a bogus "A1" dimension record,
            # which read-only mode trusts — recompute from the actual data
            ws.reset_dimensions()
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        else:
            import xlrd
            book = xlrd.open_workbook(file_contents=content)
            sh = book.sheet_by_index(0)
            rows = [sh.row_values(i) for i in range(sh.nrows)]
        if not rows:
            return []
        header = [str(h or "").strip() for h in rows[0]]

        def col(name_part: str) -> int | None:
            for i, h in enumerate(header):
                if name_part.lower() in h.lower():
                    return i
            return None

        i_date, i_cycle, i_point = col("Flow Date"), col("Cycle"), col("Receipt Point")
        i_sched = col("On-System Latest Sched")
        i_gross = col("Gross Operating Cap")
        i_oper = col("On-System Operating Cap")
        # pre-2003 workbooks are a different report: text cycles ("Timely"),
        # Excel-serial dates, and Available/Confirmed/Allocated/Operating Max
        i_alloc, i_max = col("Allocated"), col("Operating Max")
        i_avail, i_conf = col("Available Capacity"), col("Confirmed")
        legacy = i_sched is None and i_alloc is not None
        if None in (i_date, i_cycle, i_point) or (i_sched is None and not legacy):
            log.warning("socal: unexpected archive layout for %d-%02d: %s", year, month, header[:6])
            return []
        out = []
        for r in rows[1:]:
            if legacy:
                if str(r[i_cycle] or "").strip().lower() != "timely":
                    continue
            else:
                try:
                    cycle_val = str(int(float(r[i_cycle] or 0)))
                except (ValueError, TypeError):
                    continue
                if cycle_val != "1":  # timely only for deep history
                    continue
            raw = r[i_date]
            if hasattr(raw, "strftime"):  # openpyxl returns datetimes
                day_iso = raw.strftime("%Y-%m-%d")
            elif isinstance(raw, (int, float)):  # xlrd returns Excel serials
                day_iso = (date(1899, 12, 30) + timedelta(days=int(raw))).isoformat()
            else:
                day_iso = to_iso(str(raw or "").strip())
            if not re.match(r"\d{4}-\d{2}-\d{2}", day_iso):
                continue
            name = str(r[i_point] or "").strip()
            if not name:
                continue
            if legacy:
                out.append(CapacityRecord(
                    pipeline=self.name, gas_day=day_iso[:10], cycle="timely",
                    location_type="zone" if name.lower() == "total system" else "point",
                    location_id=_slug(name), location_name=name,
                    operating_cap=_num(r[i_max]) if i_max is not None else None,
                    scheduled_qty=_num(r[i_alloc]),
                    available_cap=_num(r[i_avail]) if i_avail is not None else None,
                    unit="Dth",
                    extra={"confirmed": _num(r[i_conf]) if i_conf is not None else None,
                           "source": "monthly_archive_legacy"},
                ))
            else:
                out.append(CapacityRecord(
                    pipeline=self.name, gas_day=day_iso[:10], cycle="timely",
                    location_type="point", location_id=_slug(name), location_name=name,
                    operating_cap=_num(r[i_gross]) if i_gross is not None else None,
                    scheduled_qty=_num(r[i_sched]) if i_sched is not None else None,
                    unit="Dth",
                    extra={"on_system_operating_cap":
                           _num(r[i_oper]) if i_oper is not None else None,
                           "source": "monthly_archive"},
                ))
        return out

    # -- capacity ---------------------------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        cycles = [("1", "timely"), ("2", "evening")]
        if start and end:
            cycles = [("1", "timely")]
        for d in day_range(gas_day, start, end):
            for cycle_val, cycle in cycles:
                # Must POST the filter form (as the Envoy UI does): a GET with
                # query params is silently ignored and the server returns the
                # current-day snapshot regardless of the requested gas day.
                resp = client.post(
                    f"{BASE}/ViewExternalCapacity.submitCapacitySaveAs",
                    data={"gasFlowDate": _fmt(d), "cycle": cycle_val,
                          "HiddenGasFlowDateField": _fmt(d), "HiddenCycleField": cycle_val,
                          "FileName": "capacity", "Class": CSV_CLASS},
                )
                rows = list(csv.DictReader(io.StringIO(resp.text)))
                if not rows:
                    continue
                if cycle_val == "1":
                    result.raw_paths.append(client.archive(
                        self.name, "capacity", f"{d.isoformat()}_{cycle}.csv", resp.content))
                for row in rows:
                    raw_name = row.get("Receipt Point") or ""
                    name = raw_name.strip()
                    if not name:
                        continue
                    indent = len(raw_name) - len(raw_name.lstrip())
                    result.capacity.append(CapacityRecord(
                        pipeline=self.name, gas_day=d.isoformat(), cycle=cycle,
                        location_type="point" if indent else "zone",
                        location_id=_slug(name), location_name=name,
                        operating_cap=_num(row.get("On-System Gross Operating Capacity (Dth)")),
                        scheduled_qty=_num(row.get("On-System Latest Scheduled (Dth)")),
                        unit="Dth",
                        extra={
                            "off_system_scheduled": _num(row.get("Off-System Latest Scheduled (Dth)")),
                            "min_flow_requirement": _num(row.get("On-System Minimum Flow Requirement (Dth)")),
                            "off_system_operating_cap": _num(row.get("Off-System Operating Capacity (Dth)")),
                            "indent": indent,
                        },
                    ))
        return result

    # -- flows (daily operations) -------------------------------------------------

    def fetch_flows(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for d in day_range(gas_day, start, end):
            resp = client.get(
                f"{BASE}/ViewExternalDailyOperations.submitDailyOperationsSaveAs",
                params={"estimateDate": _fmt(d), "FileName": "dailyops", "Class": CSV_CLASS},
            )
            rows = list(csv.reader(io.StringIO(resp.text)))
            if len(rows) < 3:
                continue
            result.raw_paths.append(client.archive(
                self.name, "flows", f"dailyops_{d.isoformat()}.csv", resp.content))
            # header: '', 'Actual (Dth) 07/09/2026', 'Estimate (Dth) 07/10/2026', 'Forecast...'
            columns = []
            for cell in rows[0][1:]:
                m = re.match(r"(Actual|Estimate|Forecast)[^0-9]*(\d{2}/\d{2}/\d{4})", cell or "")
                columns.append((m.group(1).lower(), to_iso(m.group(2))) if m else None)
            section = ""
            for row in rows[1:]:
                if not row or not (row[0] or "").strip():
                    continue
                label = row[0].strip()
                values = row[1:]
                if all(not (v or "").strip() for v in values):
                    section = label  # e.g. Receipts / Sendout
                    continue
                for col, cell in zip(columns, values):
                    if col is None:
                        continue
                    kind, day_iso = col
                    value = _num(cell)
                    if value is None or not day_iso:
                        continue
                    result.flows.append(FlowRecord(
                        pipeline=self.name, gas_day=day_iso, area=_slug(label),
                        flow=value, unit="Dth", kind=kind,
                        extra={"section": section, "label": label},
                    ))
        return result

    # -- maintenance ---------------------------------------------------------------

    def fetch_maintenance(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        d0 = start or gas_day
        d1 = end or gas_day + timedelta(days=120)
        resp = client.get(
            f"{BASE}/ViewExternalSystemMaintenance.submitMaintenanceSaveAs",
            params={"fromDate": _fmt(d0), "toDate": _fmt(d1), "point_type": "",
                    "FileName": "MaintenanceSchedules", "Class": CSV_CLASS},
        )
        result.raw_paths.append(client.archive(
            self.name, "maintenance", f"maint_{gas_day.isoformat()}.csv", resp.content))
        for row in csv.DictReader(io.StringIO(resp.text)):
            event_id = (row.get("Event Id") or "").strip()
            if not event_id:
                continue
            desc = (row.get("Description") or "").strip()
            loc = (row.get("Location") or "").strip()
            result.notices.append(NoticeRecord(
                pipeline=self.name, notice_id=event_id, category="maintenance",
                subject=f"{loc}: {desc[:200]}",
                body_text=desc,
                effective_start=to_iso(row.get("Start Date")),
                effective_end=to_iso(row.get("End Date")),
                posted_at=to_iso((row.get("Published Timestamp") or "").replace(" PCT", "")),
                url=f"{BASE}/ViewExternalSystemMaintenance.getMaintenanceLedger",
                extra={"location_type": (row.get("Location Type") or "").strip(),
                       "capacity_reduction_dth": _num(row.get("Capacity Reduction (Dth)")),
                       "maintenance_type": (row.get("Maintenance Type") or "").strip(),
                       "status": (row.get("Status") or "").strip()},
            ))
        return result

    # -- notices ---------------------------------------------------------------------

    _LEDGER_ROW = re.compile(
        r'\{"Message Id"\s*:\s*"([^"]+)",\s*"Message Subject":\s*"([^"]*)",'
        r'\s*"Category":\s*.([^\'"]*).,\s*"Date Posted":\s*"([^"]*)"')

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        d0 = start or gas_day - timedelta(days=30)
        d1 = end or gas_day
        for folder_id, category in NOTICE_FOLDERS.items():
            resp = client.get(
                f"{BASE}/ViewExternalEbb.getMessageLedger",
                params={"folderId": folder_id, "datePosted_from": _fmt(d0),
                        "datePosted_to": _fmt(d1), "keyword": "",
                        "Page": "filter", "ledgerType": "message"},
            )
            result.raw_paths.append(client.archive(
                self.name, "notices", f"folder{folder_id}_{gas_day.isoformat()}.html",
                resp.content))
            for m in self._LEDGER_ROW.finditer(resp.text):
                msg_id, subject, cat_label, posted = m.groups()
                # OFO declarations arrive as critical notices — give them their own category
                cat = "ofo" if re.search(r"\bOFO\b", subject) else category
                result.notices.append(NoticeRecord(
                    pipeline=self.name, notice_id=msg_id, category=cat,
                    subject=subject.strip()[:300],
                    posted_at=to_iso(posted.replace(" PCT", "")),
                    url=f"{BASE}/ViewExternalEbb.getMessageDetail?msg_id={msg_id}",
                    extra={"folder": cat_label},
                ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        bodies: dict[str, str] = {}
        for n in notices:
            url = n.get("url") or ""
            if "getMessageDetail" not in url:
                continue
            resp = client.get(url)
            doc = lxml_html.fromstring(resp.content)
            for bad in doc.xpath("//script | //style"):
                bad.getparent().remove(bad)
            text = re.sub(r"[ \t]+", " ", doc.text_content())
            body = re.sub(r"\n{3,}", "\n\n", text).strip()
            if body:
                bodies[n["notice_id"]] = body[:30000]
        return bodies
