"""Ruby Pipeline (Tallgrass) — pipeline.tallgrassenergylp.com.

The Tallgrass EBB sits behind Imperva Incapsula, which JS-challenges scripted
HTTP clients. All datasets therefore run through headless Playwright (the one
pipeline in this project needing browser emulation). Playwright is an optional
dependency: pip install playwright && playwright install chromium.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import date

from lxml import html as lxml_html

from ..dates import to_iso
from ..http import EbbClient
from ..models import CapacityRecord, NoticeRecord, RateDoc
from .base import FetchResult, PipelineAdapter
from .common import clean_body, num as _num, pdf_body_text, row_cells

log = logging.getLogger("gaswatch.ruby")

BASE = "https://pipeline.tallgrassenergylp.com"
PIPELINE_ID = 325  # Ruby

NOTICE_TYPES = {
    "CRIT": "critical",
    "NONCRIT": "non_critical",
    "PLANNED": "planned_outage",
    "TARF": "tariff",
}

# ddlCycle values on Point/Segment pages (from page markup)
CYCLES = {"0": "best_available", "1": "timely", "2": "evening", "3": "id1", "4": "id2", "6": "id3"}


@contextmanager
def _browser_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "ruby adapter needs Playwright: pip install playwright && playwright install chromium"
        ) from exc
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def _goto(page, url: str) -> None:
    """Navigate and wait out the Incapsula challenge if it appears."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    for _ in range(3):
        if "_Incapsula_Resource" in page.content():
            page.wait_for_timeout(3000)
            page.reload(wait_until="domcontentloaded")
        else:
            break
    page.wait_for_load_state("networkidle", timeout=60000)


class RubyAdapter(PipelineAdapter):
    name = "ruby"
    DATASETS = {
        "capacity": "fetch_capacity",   # operationally available, segment level
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values (9 MB tariff via browser)
    }
    # everything needs the browser (Incapsula) — pull-all skips these
    # unless --include-browser is passed
    BROWSER_DATASETS = {"capacity", "notices", "rates", "rate_values"}
    HEAVY_DATASETS = ("rate_values",)

    # -- notices -----------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        with _browser_page() as page:
            for type_code, category in NOTICE_TYPES.items():
                url = f"{BASE}/Pages/Notices.aspx?pipeline={PIPELINE_ID}&type={type_code}"
                _goto(page, url)
                content = page.content()
                result.raw_paths.append(EbbClient.archive(
                    self.name, "notices", f"{type_code}_{gas_day.isoformat()}.html",
                    content.encode()))
                doc = lxml_html.fromstring(content)
                # grid columns: NoticeType | SubType | PostDate | EffDate | EndDate | ID | Subject
                for tr in doc.xpath('//table[@id="mainContent_GridView1"]//tr[td]'):
                    cells = row_cells(tr)
                    if len(cells) < 7 or not cells[5].isdigit():
                        continue
                    result.notices.append(NoticeRecord(
                        pipeline=self.name, notice_id=cells[5], category=category,
                        subject=cells[6][:300],
                        effective_start=to_iso(cells[3]),
                        effective_end=to_iso(cells[4]),  # 12/31/9000 sentinel -> ''
                        posted_at=to_iso(cells[2]), url=url,
                        extra={"notice_type": cells[0], "notice_subtype": cells[1],
                               "list_type": type_code},
                    ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        """Open each notice's detail view; the body is usually a PDF in an iframe."""
        wanted_by_type: dict[str, set[str]] = {}
        for n in notices:
            list_type = (n.get("extra") or {}).get("list_type", "CRIT")
            wanted_by_type.setdefault(list_type, set()).add(n["notice_id"])
        bodies: dict[str, str] = {}
        with _browser_page() as page:
            for type_code, wanted in wanted_by_type.items():
                url = f"{BASE}/Pages/Notices.aspx?pipeline={PIPELINE_ID}&type={type_code}"
                for notice_id in wanted:
                    try:
                        body = self._fetch_one_body(page, url, notice_id)
                    except Exception as exc:
                        log.warning("ruby: body fetch failed for notice %s: %s", notice_id, exc)
                        continue
                    if body:
                        bodies[notice_id] = body
        return bodies

    @staticmethod
    def _fetch_one_body(page, list_url: str, notice_id: str) -> str:
        _goto(page, list_url)
        row = page.locator(f'#mainContent_GridView1 tr:has(td:text-is("{notice_id}"))')
        if not row.count():
            return ""
        link = row.first.locator("a")
        if not link.count():
            return ""
        link.first.click()
        page.wait_for_url("**/NoticeDetail.aspx*", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=60000)
        # PDF notice: iframe#pdf points at the document
        pdf_src = page.locator("iframe#pdf").get_attribute("src", timeout=5000) \
            if page.locator("iframe#pdf").count() else None
        if pdf_src:
            resp = page.request.get(BASE + pdf_src if pdf_src.startswith("/") else pdf_src)
            if resp.ok:
                try:
                    text = pdf_body_text(resp.body())
                    if text:
                        return text
                except Exception as exc:
                    log.warning("ruby: pdf extract failed for %s: %s", notice_id, exc)
        # text notice: DetailsView table
        detail = page.locator("#mainContent_DetailsView1")
        if detail.count():
            return clean_body(detail.inner_text(timeout=10000))
        return ""

    # -- capacity (segment, operationally available) -------------------------

    # cycles pulled per gas day; "Best Available" (0) renders an empty grid, so
    # pull the concrete cycles instead
    CAPACITY_CYCLES = [("1", "timely"), ("2", "evening")]

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        url = f"{BASE}/Pages/Segment.aspx?pipeline={PIPELINE_ID}&type=OA"
        day_str = f"{gas_day.month}/{gas_day.day}/{gas_day.year}"
        with _browser_page() as page:
            for cycle_val, cycle in self.CAPACITY_CYCLES:
                _goto(page, url)
                page.wait_for_selector("#mainContent_tbGasFlow", timeout=30000)
                page.fill("#mainContent_tbGasFlow", day_str)
                page.select_option("#mainContent_ddlCycle", cycle_val)
                page.click("#mainContent_btnRetrieve")
                page.wait_for_load_state("networkidle", timeout=60000)
                page.wait_for_timeout(1500)
                content = page.content()
                result.raw_paths.append(EbbClient.archive(
                    self.name, "capacity", f"segment_{gas_day.isoformat()}_{cycle}.html",
                    content.encode()))
                doc = lxml_html.fromstring(content)
                n = 0
                # grid columns: Detail | LocSegment | SegmentDescription | AllQtyAvail |
                # DesignCap | UnsubscribedCap | OperatingCap | TotalSchedQty | OAC |
                # FlowIndDesc | IT | QtyReason
                for tr in doc.xpath('//table[@id="mainContent_GridView1"]//tr[td]'):
                    cells = row_cells(tr)
                    if len(cells) < 11 or not cells[1]:
                        continue
                    result.capacity.append(CapacityRecord(
                        pipeline=self.name, gas_day=gas_day.isoformat(), cycle=cycle,
                        location_type="segment", location_id=cells[1],
                        location_name=cells[2],
                        design_cap=_num(cells[4]), operating_cap=_num(cells[6]),
                        scheduled_qty=_num(cells[7]), available_cap=_num(cells[8]),
                        unit="Dth", flow_direction=cells[9],
                        extra={"unsubscribed_cap": _num(cells[5]),
                               "all_qty_avail": cells[3], "it": cells[10]},
                    ))
                    n += 1
                if n == 0:
                    log.warning("ruby: no capacity rows for %s cycle %s (raw HTML archived)",
                                gas_day, cycle)
        return result

    # -- rates / tariff -----------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        with _browser_page() as page:
            _goto(page, f"{BASE}/Pages/Notices.aspx?pipeline={PIPELINE_ID}&type=TARF")
            resp = page.request.get(f"{BASE}/Content/RUBY/RUBY_tariff.pdf")
            body = resp.body() if resp.ok else b""
            result.rate_docs.append(RateDoc(
                pipeline=self.name, doc_type="tariff", title="Ruby entire tariff (PDF)",
                url=f"{BASE}/Content/RUBY/RUBY_tariff.pdf",
                content_hash=hashlib.sha1(body).hexdigest() if body else "",
            ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Parse the Section 1 service-rate pages of the tariff PDF (browser fetch)."""
        from .. import ratesheets
        result = FetchResult()
        url = f"{BASE}/Content/RUBY/RUBY_tariff.pdf"
        with _browser_page() as page:
            _goto(page, f"{BASE}/Pages/Notices.aspx?pipeline={PIPELINE_ID}&type=TARF")
            resp = page.request.get(url)
            if not resp.ok:
                raise RuntimeError(f"ruby tariff download failed: HTTP {resp.status}")
            content = resp.body()
        texts = ratesheets.pdf_page_texts(content, list(range(60)))
        texts = {i: t for i, t in texts.items()
                 if "STATEMENT OF RATES" in t and "Section 1 - Service Rates" in t}
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_ruby(
            texts, source_url=url))
        return result
