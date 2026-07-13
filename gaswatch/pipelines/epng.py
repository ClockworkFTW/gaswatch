"""El Paso Natural Gas (Kinder Morgan) — DART informational postings.

Server-rendered Infragistics grids, anonymous GET. seg_nbr=0 returns ALL
segments/points; grids page at 75 rows, and further pages are fetched by
replaying the grid's ASP.NET postback with a Paging/PageChange event in the
grid clientState (protocol captured from a live browser session).
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
from .common import clean_body, day_range, num as _num, probe_content_hash, row_cells

log = logging.getLogger("gaswatch.epng")

BASE = "https://pipeline2.kindermorgan.com"
CODE = "EPNG"

# Captured from live DART sessions: each grid accepts its exact clientState
# with a Paging/PageChange event ({page} = zero-based page index) POSTed with
# the page's form fields plus __EVENTTARGET/__EVENTARGUMENT. The behavior list
# is positional and grid-specific, hence one template per page type.
CS_SEGMENT = (
    '[[[[null,75,null]],[[[[[0]],[],null],[null,null],[null]],[[[[7]],[],null],[null,null],[null]],'
    '[[[["ColumnMoving"]],[],[{{}}]],[{{}},[{{}}]],null],[[[["ColumnResizing",null]],[],[{{}}]],[{{}},[{{}}]],null],'
    '[[[["ColumnFixing",null]],[[[[[]],[],null],[null,null],[null]],[[[[]],[],null],[null,null],[null]]],'
    '[{{"0":[[0]]}}]],[{{}},[{{}}]],null],[[[["Filtering",null]],[[[[[null,null,null]],[],[]],[{{}},[]],null]],'
    '[{{"View":[[]],"seg_its_ind":[[]]}}]],[{{}},[{{}}]],null],[[[["Paging"]],[],[]],[{{}},[]],null],'
    '[[[["Sorting"]],[],[{{"View":[[0]]}}]],[{{}},[{{}}]],null],'
    '[[[["Selection",null,null,1,null,null,null]],[],[]],[{{}},[]],null]],null],[{{}},[{{}},{{}}]],'
    '[{{"ownerName":"Paging","type":"PageChange","id":null,"value":{page},"tag":null}}]]'
)
CS_POINT = (
    '[[[[null,75,null]],[[[[[0]],[],null],[null,null],[null]],[[[[7]],[],null],[null,null],[null]],'
    '[[[["ColumnMoving"]],[],[{{}}]],[{{}},[{{}}]],null],[[[["ColumnResizing",null]],[],[{{}}]],[{{}},[{{}}]],null],'
    '[[[["Filtering",null]],[[[[[null,null,null]],[],[]],[{{}},[]],null]],'
    '[{{"View":[[]],"all_quan_avail":[[]]}}]],[{{}},[{{}}]],null],[[[["Paging"]],[],[]],[{{}},[]],null],'
    '[[[["Sorting"]],[],[{{"View":[[0]],"all_quan_avail":[[0]]}}]],[{{}},[{{}}]],null],'
    '[[[["Selection",null,null,1,null,null,null]],[],[]],[{{}},[]],null],'
    '[[[["ColumnFixing",null]],[[[[[]],[],null],[null,null],[null]],[[[[]],[],null],[null,null],[null]]],'
    '[{{"0":[[0]]}}]],[{{}},[{{}}]],null]],null],[{{}},[{{}},{{}}]],'
    '[{{"ownerName":"Paging","type":"PageChange","id":null,"value":{page},"tag":null}}]]'
)
GRID_ID = "WebSplitter1_tmpl1_ContentPlaceHolder1_DGOpAvail"

NOTICE_TYPES = {"C": "critical", "N": "non_critical", "P": "planned_outage"}


def _fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


class EpngAdapter(PipelineAdapter):
    name = "epng"
    DATASETS = {
        "capacity": "fetch_capacity",   # segments (incl. scheduled quantities)
        "points": "fetch_points",       # point-level capacity/scheduled
        "notices": "fetch_notices",
        "rates": "fetch_rates",
        "rate_values": "fetch_rate_values",  # parsed $ values (45 MB tariff download)
    }
    HEAVY_DATASETS = ("rate_values",)

    # -- grid plumbing -------------------------------------------------------

    @staticmethod
    def _grid_rows(doc) -> list[list[str]]:
        rows = []
        for tr in doc.xpath("//tr[td[@data-ig]]"):
            if len(tr.xpath("./td[@data-ig]")) >= 8:
                rows.append(row_cells(tr))
        return rows

    def _fetch_all_pages(self, client: EbbClient, url: str, cs_template: str,
                         max_pages: int = 40):
        """Yield (page_doc, rows) for every grid page of a DART posting URL."""
        resp = client.get(url)
        doc = lxml_html.fromstring(resp.text)
        rows = self._grid_rows(doc)
        yield doc, rows
        m = re.search(r"Row Count:\s*(\d+)", resp.text)
        total = int(m.group(1)) if m else len(rows)
        page_size = len(rows)
        if page_size == 0 or total <= page_size:
            return
        n_pages = min(-(-total // page_size), max_pages)
        form = {}
        for inp in doc.xpath("//input[@name]"):
            itype = (inp.get("type") or "").lower()
            if itype in ("radio", "checkbox") and inp.get("checked") is None:
                continue
            if itype in ("submit", "button", "image"):
                continue
            form[inp.get("name")] = inp.get("value") or ""
        form["__EVENTTARGET"] = GRID_ID
        form["__EVENTARGUMENT"] = "PageIndexChanging"
        for page in range(1, n_pages):
            form_p = dict(form)
            form_p[f"{GRID_ID}_clientState"] = cs_template.format(page=page)
            resp = client.post(url, data=form_p, headers={"Referer": url})
            doc_p = lxml_html.fromstring(resp.text)
            rows_p = self._grid_rows(doc_p)
            if not rows_p:
                log.warning("epng: paging returned no rows for %s page %d", url, page)
                break
            yield doc_p, rows_p

    CYCLE_SLUGS = {
        "timely": "timely", "evening": "evening", "best available": "best_available",
        "intraday 1": "id1", "intraday 2": "id2", "intraday 3": "id3",
        "cycle 6": "cycle6", "cycle 7": "cycle7",
    }

    @classmethod
    def _cycle_of(cls, doc) -> str:
        m = re.search(r"CycleDesc:\s*([A-Z0-9 ]+?)\s*Post Date", doc.text_content())
        raw = m.group(1).strip().lower() if m else ""
        return cls.CYCLE_SLUGS.get(raw, raw.replace(" ", "_") or "best_available")

    # -- capacity: segments ---------------------------------------------------

    def fetch_capacity(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        return self._fetch_days(client, gas_day, "segment", start, end)

    def fetch_points(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        return self._fetch_days(client, gas_day, "point", start, end)

    def _fetch_days(self, client: EbbClient, gas_day: date, loc_type: str,
                    start, end) -> FetchResult:
        # past flow_days return the settled CYCLE 7 view — history is backfillable
        result = FetchResult()
        for d in day_range(gas_day, start, end):
            try:
                one = self._fetch_opavail(client, d, loc_type)
            except Exception as exc:
                log.warning("epng: %s fetch failed for %s (%s)", loc_type, d, exc)
                continue
            result.capacity.extend(one.capacity)
            result.raw_paths.extend(one.raw_paths)
        return result

    def _fetch_opavail(self, client: EbbClient, gas_day: date, loc_type: str) -> FetchResult:
        result = FetchResult()
        page_name = "OpAvailSegment" if loc_type == "segment" else "OpAvailPoint"
        cs_template = CS_SEGMENT if loc_type == "segment" else CS_POINT
        url = (f"{BASE}/Capacity/{page_name}.aspx?code={CODE}"
               f"&flow_day={_fmt_day(gas_day)}&seg_nbr=0&type=D&f=x")
        for page_idx, (doc, rows) in enumerate(self._fetch_all_pages(client, url, cs_template)):
            cycle = self._cycle_of(doc)
            if page_idx == 0:
                result.raw_paths.append(client.archive(
                    self.name, loc_type, f"{gas_day.isoformat()}_{cycle}_p{page_idx}.html",
                    lxml_html.tostring(doc)))
            for cells in rows:
                # segment: View, Loc, Name, Zn, DC, OC, TSQ, OAC, IT, FlowInd, AllQty, Reason
                # point:   View, Loc, Name, Zn, Loc(Segment), DC, OC, TSQ, OAC, IT, FlowInd, AllQty, Reason
                if loc_type == "segment" and len(cells) >= 11:
                    (_, loc, name, zone, dc, oc, tsq, oac, it, flow_ind, *_rest) = cells[:11]
                    seg = loc
                elif loc_type == "point" and len(cells) >= 12:
                    (_, loc, name, zone, seg, dc, oc, tsq, oac, it, flow_ind, *_rest) = cells[:12]
                else:
                    continue
                if not loc or not loc.isdigit():
                    continue
                result.capacity.append(CapacityRecord(
                    pipeline=self.name, gas_day=gas_day.isoformat(), cycle=cycle,
                    location_type=loc_type, location_id=loc, location_name=name,
                    design_cap=_num(dc), operating_cap=_num(oc),
                    scheduled_qty=_num(tsq), available_cap=_num(oac),
                    unit="Dth", flow_direction=flow_ind,
                    extra={"zone": zone, "it": it, "segment": seg},
                ))
        return result

    # -- notices ---------------------------------------------------------------

    def fetch_notices(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        for type_code, category in NOTICE_TYPES.items():
            url = f"{BASE}/Notices/Notices.aspx?type={type_code}&code={CODE}"
            # first page only: latest 75 notices per type, newest first —
            # recurring pulls accumulate history in the DB
            resp = client.get(url)
            doc = lxml_html.fromstring(resp.text)
            for cells in self._grid_rows(doc):
                # Type1, Type2, PostDate, EffDate, EndDate, NoticeID, Subject, ...
                if len(cells) < 7 or not cells[5].strip().isdigit():
                    continue
                type1, type2, posted, eff, end_dt, notice_id, subject = cells[:7]
                post_date = posted.split(" ")[0] if posted else _fmt_day(gas_day)
                detail_url = (f"{BASE}/Notices/NoticeDetail.aspx?code={CODE}"
                              f"&notc_nbr={notice_id}&date={post_date}"
                              f"&notc_type=-1&notc_sub_type=-1&notc_ind={type_code}")
                result.notices.append(NoticeRecord(
                    pipeline=self.name, notice_id=notice_id, category=category,
                    subject=subject, effective_start=to_iso(eff),
                    effective_end=to_iso(end_dt), posted_at=to_iso(posted),
                    url=detail_url,
                    extra={"notice_type": type1, "notice_subtype": type2},
                ))
        return result

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        bodies: dict[str, str] = {}
        for n in notices:
            url = n.get("url") or ""
            if "NoticeDetail.aspx" not in url:
                continue
            resp = client.get(url)
            doc = lxml_html.fromstring(resp.text)
            text = re.sub(r"[ \t]+", " ", doc.text_content())
            if "Notice Text:" not in text:
                continue
            body = text.split("Notice Text:", 1)[1]
            # trim the page footer/nav that follows the notice content
            for marker in ("Would you like", "Site Map", "©", "DART Login"):
                idx = body.find(marker)
                if idx > 0:
                    body = body[:idx]
            body = clean_body(body)
            if body:
                bodies[n["notice_id"]] = body
        return bodies

    # -- rates / tariff ----------------------------------------------------------

    def fetch_rates(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        result = FetchResult()
        url = f"{BASE}/Tariff/SubIndex.aspx?code={CODE}&category=CER"
        resp = client.get(url)
        doc = lxml_html.fromstring(resp.text)
        # the CER page is a tree of section links, each a named destination
        # inside the entire-tariff PDF (e.g. ...EntireTariff.pdf#cerpasr)
        for a in doc.xpath("//table[contains(@id,'tblContacts')]//a[@href]"):
            title = re.sub(r"\s+", " ", a.text_content()).strip()
            href = a.get("href") or ""
            if not title or "#" not in href:
                continue
            if href.startswith("/"):
                href = BASE + href
            result.rate_docs.append(RateDoc(
                pipeline=self.name, doc_type="rates", title=title[:200], url=href,
            ))
        # 45 MB entire-tariff PDF — track version without downloading it
        tariff_url = f"{BASE}/Documents/EPNG/EPNG_EntireTariff.pdf"
        result.rate_docs.append(RateDoc(
            pipeline=self.name, doc_type="tariff", title="EPNG entire tariff (PDF)",
            url=tariff_url,
            content_hash=probe_content_hash(client, tariff_url),
        ))
        return result

    def fetch_rate_values(self, client: EbbClient, gas_day: date, *, start=None, end=None) -> FetchResult:
        """Download the entire tariff and parse the CER statement-of-rates pages.

        The CER sections are named destinations (cer*) inside the PDF, so the
        page numbers track tariff refilings automatically.
        """
        from .. import ratesheets
        result = FetchResult()
        url = f"{BASE}/Documents/EPNG/EPNG_EntireTariff.pdf"
        content = client.get(url).content
        pages = sorted(set(ratesheets.pdf_named_dest_pages(content, "cer").values()))
        if not pages:
            raise RuntimeError("no cer* named destinations in the EPNG tariff PDF")
        texts = ratesheets.pdf_page_texts(content, pages)
        result.rate_values = ratesheets.validated(self.name, ratesheets.parse_epng(texts, source_url=url))
        return result
