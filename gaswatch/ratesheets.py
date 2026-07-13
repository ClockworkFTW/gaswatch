"""Parse actual rate values (reservation, usage, fuel, ...) out of rate documents.

Every parser here is text-based: the adapter downloads the PDF, extracts page
text with pypdf, and hands the text here. That keeps parsers unit-testable
against saved text fixtures without multi-MB PDF fixtures.

Rate sheets are typeset for humans, so each parser is deliberately
conservative: it only emits a value when the line matches the layout the
parser was written against, and drops anything ambiguous. A tariff refiling
that changes the layout shows up as a record-count drop in pull_log, not as
silently wrong numbers.
"""
from __future__ import annotations

import io
import logging
import re

from .models import TariffRate

log = logging.getLogger("gaswatch.ratesheets")

_MONEY = re.compile(r"\$?\s?(-?[\d,]+\.\d+)")


def _money(tok: str) -> float | None:
    m = _MONEY.search(tok)
    return float(m.group(1).replace(",", "")) if m else None


_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}


def _date(text: str) -> str:
    """First date in text -> ISO, '' if none. Tolerates PDF-extraction
    artifacts like intra-word spaces ("J anuary 1, 2026")."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        mo, d, y = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    squashed = re.sub(r"(?<=[A-Za-z])\s+(?=[a-z])", "", text)
    m = re.search(r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
                  r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|"
                  r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})",
                  squashed)
    if m:
        mo = _MONTH_NUM[m.group(1)[:3].lower()]
        return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    return ""


def pdf_page_texts(content: bytes, pages: list[int] | None = None,
                   layout: bool = False) -> dict[int, str]:
    """Extract text for the given (or all) pages of a PDF."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    idx = pages if pages is not None else range(len(reader.pages))
    out = {}
    for i in idx:
        if 0 <= i < len(reader.pages):
            page = reader.pages[i]
            text = (page.extract_text(extraction_mode="layout") if layout
                    else page.extract_text()) or ""
            if layout and not text.strip():
                text = page.extract_text() or ""
            out[i] = text
    return out


def pdf_named_dest_pages(content: bytes, prefix: str) -> dict[str, int]:
    """Named destinations starting with prefix -> page number."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    out = {}
    for name, dest in reader.named_destinations.items():
        if name.lower().startswith(prefix):
            try:
                out[name] = reader.get_destination_page_number(dest)
            except Exception:  # noqa: BLE001 — malformed dest, skip
                continue
    return out


# -- CGT (PG&E) annual rate sheet: one poster page, three columns ----------------

_CGT_HEADER = re.compile(r"RATES\s+\((G-[A-Z]+)\)\s*-?\s*(.*)")
_CGT_CHARGE = re.compile(
    r"(Reservation Charge|Usage Charge|Total|Contract Inventory|"
    r"Injection \(per day\)|Withdrawal \(per day\)|Inventory)\s+"
    r"\((\$[^)]*)\)\s+([\d.,]+|n/a)")
_CGT_COMPONENT = {
    "Reservation Charge": "reservation", "Usage Charge": "usage", "Total": "total",
    "Contract Inventory": "inventory", "Inventory": "inventory",
    "Injection (per day)": "injection", "Withdrawal (per day)": "withdrawal",
}


def parse_cgt(layout_text: str, source_url: str = "") -> list[TariffRate]:
    """PG&E backbone/storage rate sheet (rates<year>.pdf), layout-mode text.

    The poster has three equal-width columns; each is parsed as its own
    top-to-bottom stream.
    """
    lines = layout_text.splitlines()
    width = max((len(ln) for ln in lines), default=0)
    bounds = [(0, width // 3), (width // 3, 2 * width // 3), (2 * width // 3, width + 1)]
    year = ""
    m = re.search(r"RATES\s+\S\s+(\d{4})", layout_text)
    if m:
        year = m.group(1)
    effective = f"{year}-01-01" if year else ""

    out: list[TariffRate] = []
    for lo, hi in bounds:
        schedule, section, label = "", "", ""
        for raw in lines:
            cell = raw[lo:hi].strip()
            if not cell or cell == year:
                continue
            hm = _CGT_HEADER.search(cell)
            if hm:
                schedule, section, label = hm.group(1), "", ""
                continue
            if schedule and re.match(r"^[A-Z][A-Z &/-]+(DELIVERIES|DESIGN|RATES?)\b", cell):
                dm = re.search(r"\b([SM]FV)\b", cell)
                if dm:
                    section = dm.group(1)
                continue
            cm = _CGT_CHARGE.search(cell)
            if cm and schedule:
                comp_label, unit, value = cm.groups()
                if value == "n/a":
                    continue
                path = label + (f" ({section})" if section else "")
                out.append(TariffRate(
                    pipeline="cgt", rate_schedule=schedule,
                    component=_CGT_COMPONENT[comp_label], path=path, qualifier="max",
                    value=float(value.replace(",", "")), unit=unit,
                    effective_date=effective, source_url=source_url))
            elif schedule and not cm:
                # context label: "Redwood to On-System Core", "Volumetric Rate", ...
                if len(cell) < 60 and not cell.startswith(("RATES", "(")):
                    label = cell
    return out


# -- EPNG statement of rates: zone matrices inside the entire-tariff PDF ---------

_EPNG_SCHED_ROW = re.compile(r"^(?:[A-Z]{2,5}[A-Z0-9-]*\s+){2,}[A-Z]{2,5}[A-Z0-9-]*\s*$")


def _epng_values(line: str) -> list[float | None]:
    return [None if t.upper() == "N/A" else _money(t)
            for t in re.findall(r"\$[\d,.]+|N/A", line, re.IGNORECASE)]


def parse_epng(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """EPNG Part II statement-of-rates pages (zone pages, IT/PAL, fuel)."""
    out: list[TariffRate] = []
    for text in pages.values():
        effective = ""
        m = re.search(r"Effective on:\s*(.+)", text)
        if m:
            effective = _date(m.group(1))
        if "ZONAL RATES - Daily Usage" in text:
            out.extend(_parse_epng_it(text, effective, source_url))
        elif "FUEL, L&U and EPC CHARGES" in text:
            out.extend(_parse_epng_fuel(text, effective, source_url))
        elif "STATEMENT OF RATES" in text:
            out.extend(_parse_epng_zone(text, effective, source_url))
    return out


def _parse_epng_zone(text: str, effective: str, url: str) -> list[TariffRate]:
    zone = ""
    m = re.search(r"Section 1\.\d+ - (.+?) Rates", text)
    if m:
        zone = m.group(1).strip()
    lines = [ln.strip() for ln in text.splitlines()]
    schedules: list[str] = []
    out: list[TariffRate] = []
    context, in_article = "", False
    for ln in lines:
        if not schedules and _EPNG_SCHED_ROW.match(ln) and "FT" in ln:
            schedules = ln.split()
            continue
        if not schedules:
            continue
        upper = ln.upper()
        if "ARTICLE" in upper and "RESERVATION" in upper:
            in_article = True
            continue
        if upper.startswith("USAGE RATES"):
            context, in_article = "usage", False
            continue
        if upper.startswith("DAILY RESERVATION") or ln.startswith("Daily Reservation"):
            context = "reservation_daily"
            continue
        if ln.startswith("Monthly Reservation") and not in_article:
            vals = _epng_values(ln)
            out.extend(_epng_emit(zone, schedules, vals, "reservation", "$/Dth/mo",
                                  "max", effective, url))
            continue
        qm = re.match(r"^(Maximum|Minimum)\b", ln)
        if qm and context and not in_article:
            vals = _epng_values(ln)
            comp = "usage" if context == "usage" else "reservation"
            unit = "$/Dth" if context == "usage" else "$/Dth/d"
            out.extend(_epng_emit(zone, schedules, vals, comp, unit,
                                  qm.group(1).lower()[:3], effective, url))
    # system-wide balancing (FDBS) and storage (ISS) rows on the lateral page
    m = re.search(r"^FDBS((?:\s+\$[\d,.]+){5})", text, re.MULTILINE)
    if m:
        v = _epng_values(m.group(1))
        for comp, qual, val, unit in (
                ("reservation", "max", v[0], "$/Dth/mo"),
                ("reservation", "max", v[1], "$/Dth/d"), ("reservation", "min", v[2], "$/Dth/d"),
                ("usage", "max", v[3], "$/Dth"), ("usage", "min", v[4], "$/Dth")):
            # monthly + daily reservation share a component; unit disambiguates via path
            out.append(TariffRate(
                pipeline="epng", rate_schedule="FDBS", component=comp,
                path="system-wide balancing" + (" (monthly)" if unit == "$/Dth/mo" else ""),
                qualifier=qual, value=val, unit=unit, effective_date=effective,
                source_url=url))
    m = re.search(r"^ISS((?:\s+\$[\d,.]+){6})", text, re.MULTILINE)
    if m:
        v = _epng_values(m.group(1))
        for comp, mx, mn in (("inventory", v[0], v[1]), ("injection", v[2], v[3]),
                             ("withdrawal", v[4], v[5])):
            out.append(TariffRate(pipeline="epng", rate_schedule="ISS", component=comp,
                                  path="storage", qualifier="max", value=mx, unit="$/Dth",
                                  effective_date=effective, source_url=url))
            out.append(TariffRate(pipeline="epng", rate_schedule="ISS", component=comp,
                                  path="storage", qualifier="min", value=mn, unit="$/Dth",
                                  effective_date=effective, source_url=url))
    return out


def _epng_emit(zone: str, schedules: list[str], vals: list[float | None],
               comp: str, unit: str, qual: str, effective: str, url: str):
    if len(vals) != len(schedules):  # partial row (e.g. truncated article rates)
        return []
    return [TariffRate(pipeline="epng", rate_schedule=s, component=comp,
                       path=zone + (" (monthly)" if unit == "$/Dth/mo" else ""),
                       qualifier=qual, value=v, unit=unit,
                       effective_date=effective, source_url=url)
            for s, v in zip(schedules, vals) if v is not None]


def _parse_epng_it(text: str, effective: str, url: str) -> list[TariffRate]:
    schedules = ["IT-1", "IHSW", "PAL Parking", "PAL Lending"]
    out, zone = [], ""
    for ln in (s.strip() for s in text.splitlines()):
        if re.match(r"^(Production Area|Texas|New Mexico|Arizona|Nevada|California)$", ln):
            zone = ln
            continue
        qm = re.match(r"^(Maximum|Minimum)\b", ln)
        if qm and zone:
            vals = _epng_values(ln)
            if len(vals) == len(schedules):
                out.extend(TariffRate(
                    pipeline="epng", rate_schedule=s, component="usage", path=zone,
                    qualifier=qm.group(1).lower()[:3], value=v, unit="$/Dth",
                    effective_date=effective, source_url=url)
                    for s, v in zip(schedules, vals) if v is not None)
    return out


def _parse_epng_fuel(text: str, effective: str, url: str) -> list[TariffRate]:
    out = []
    for ln in (s.strip() for s in text.splitlines()):
        m = re.match(r"^([A-Za-z][A-Za-z &'/-]+?)(?:\s+\d/)?\s+"
                     r"(-?[\d.]+)%\s+(-?[\d.]+)%\s+(-?[\d.]+)%\s*$", ln)
        if m and "true" not in m.group(1).lower():
            out.append(TariffRate(
                pipeline="epng", rate_schedule="FUEL", component="fuel",
                path=m.group(1).strip(), qualifier="", value=float(m.group(4)),
                unit="%", effective_date=effective, source_url=url,
                notes="total retention (collection + true-up)"))
    return out


# -- Kern River statement of rates ------------------------------------------------

_KERN_CONTEXT = re.compile(r"^(.*?(?:Rate|Rates))\s*[:\d/ ]*$")


def parse_kern(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """Kern River sheets 5+ — skips the shipper-specific Period Two sheets."""
    out: list[TariffRate] = []
    for text in pages.values():
        if "STATEMENT OF RATES" not in text or "period two" in text.lower():
            continue
        schedule = ""
        m = re.search(r"RATE SCHEDULE\s+([A-Z0-9-]+)", text)
        if m:
            schedule = m.group(1)
        if not schedule:
            continue
        system = ""
        sm = re.search(r"^(.*(?:SYSTEM|EXPANSION|PROJECT).*)$", text, re.MULTILINE)
        if sm and len(sm.group(1).strip()) < 70:
            system = sm.group(1).strip().title()
        effective = ""
        em = re.search(r"Effective On:\s*(.+)", text)
        if em:
            effective = _date(em.group(1))
        term, component = "", ""
        for ln in (s.strip() for s in text.splitlines()):
            cm = _KERN_CONTEXT.match(ln)
            if cm and len(ln) < 60 and "$" not in ln:
                label = re.sub(r"\s*\d/\s*$|\s*:$", "", cm.group(1))
                low = label.lower()
                if "usage" in low or "commodity" in low:
                    component, term = "usage", ""
                elif "overrun" in low:
                    component, term = "overrun", ""
                elif "reservation" in low or "demand" in low:
                    component = "reservation"  # keeps the current term context
                else:
                    # term/tier context: "Recourse Rate", "10-Year Rate", ...
                    term = re.sub(r"\s*Rates?$", "", label)
                    component = ""
                continue
            qm = re.match(r"^(Maximum|Minimum)\s+\$\s?(-?[\d,.]+)", ln)
            if qm and component:
                comp = component
                path = term or "base"
                if system:
                    path = f"{path} - {system}" if term else system
                out.append(TariffRate(
                    pipeline="kernriver", rate_schedule=schedule, component=comp,
                    path=path, qualifier=qm.group(1).lower()[:3],
                    value=float(qm.group(2).replace(",", "")), unit="$/Dth/d"
                    if comp == "reservation" else "$/Dth",
                    effective_date=effective, source_url=source_url))
    return out


# -- Northwest Pipeline statement of rates ----------------------------------------

_NWP_VALUES = re.compile(r"^(.*?)\s+(\.\d{5})\s+(\.\d{5})\s*$")
_NWP_COMPONENT = re.compile(
    r"^(Reservation|Volumetric|Scheduled Overrun|Scheduled Daily Overrun|"
    r"Annual Overrun)\b")


def parse_nwp(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    out: list[TariffRate] = []
    comp_map = {"Reservation": "reservation", "Volumetric": "usage",
                "Scheduled Overrun": "overrun", "Scheduled Daily Overrun": "overrun",
                "Annual Overrun": "overrun"}
    for text in pages.values():
        if "STATEMENT OF RATES" not in text:
            continue
        schedule, component, subgroup = "", "", ""
        for ln in (s.strip() for s in text.splitlines()):
            sm = re.match(r"^Rate Schedule ([A-Z]+-\d+)", ln)
            if sm:
                schedule, component, subgroup = sm.group(1), "", ""
                # rest of the line may carry the component ("Rate Schedule TI-1 (2)")
                ln = ln[sm.end():].strip()
            vm = _NWP_VALUES.match(ln)
            label = vm.group(1).strip() if vm else ln
            label = re.sub(r"\s*\([\d), (]+\)?\s*$", "", label).strip()  # footnote refs
            cm = _NWP_COMPONENT.match(label)
            if cm:
                component = comp_map[cm.group(1)]
                subgroup = ""
                label = label[cm.end():].strip()
            elif label.startswith("(") and label.endswith(")"):
                subgroup = label.strip("()")
                label = ""
            if vm and schedule and component:
                path = " ".join(p for p in (subgroup, label) if p) or "system-wide"
                lo, hi = float(vm.group(2)), float(vm.group(3))
                unit = "$/Dth/d" if component == "reservation" else "$/Dth"
                out.append(TariffRate(pipeline="nwp", rate_schedule=schedule,
                                      component=component, path=path, qualifier="max",
                                      value=hi, unit=unit, source_url=source_url))
                out.append(TariffRate(pipeline="nwp", rate_schedule=schedule,
                                      component=component, path=path, qualifier="min",
                                      value=lo, unit=unit, source_url=source_url))
    return out


# -- GTN statement of rates --------------------------------------------------------

_GTN_BASE = re.compile(r"^\s*BASE\s+(.+)$", re.MULTILINE)
_GTN_SLOTS = (("reservation", "mileage", "$/Dth-mi/d"), ("reservation", "", "$/Dth/d"),
              ("delivery", "", "$/Dth-mi/d"))


def parse_gtn(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """GTN Part 4 statements: BASE reservation rows + fuel percentages."""
    out: list[TariffRate] = []
    for text in pages.values():
        if "Statement of Rates" not in text:
            continue
        effective = ""
        em = re.search(r"Effective:\s*(.+)", text)
        if em:
            effective = _date(em.group(1))
        # which schedules this part covers — from the volume-header line
        # ("Fourth Revised Volume No. 1-A FTS-1, LFS-1, and FHS Rates")
        hm = re.search(r"Volume No\. 1-A (.+?) Rates\b", text) or \
            re.search(r"Rate Schedules? ([A-Z0-9-]+(?:,? (?:and )?[A-Z0-9-]+)*)", text)
        schedules = re.findall(r"[A-Z]{2,4}-?\d?", hm.group(1)) if hm else []
        label = "/".join(dict.fromkeys(schedules)) or "GTN"
        for bm in _GTN_BASE.finditer(text):
            toks = bm.group(1).split()
            vals = [None if not re.match(r"^[\d.]+$", t) else float(t) for t in toks[:6]]
            if len(vals) < 6:
                continue
            pairs = [(vals[0], vals[1]), (vals[2], vals[3]), (vals[4], vals[5])]
            for (mx, mn), (comp, sub, unit) in zip(pairs, _GTN_SLOTS):
                path = ("mileage" if sub else "non-mileage") if comp == "reservation" \
                    else "delivery charge"
                for qual, val in (("max", mx), ("min", mn)):
                    if val is not None:
                        out.append(TariffRate(
                            pipeline="gtn", rate_schedule=label, component=comp,
                            path=path, qualifier=qual, value=val, unit=unit,
                            effective_date=effective, source_url=source_url))
        fm = re.search(r"MAXIMUM FUEL AND LINE LOSS PERCENTAGE\s+-*\s*([\d.]+)%", text)
        if fm:
            out.append(TariffRate(pipeline="gtn", rate_schedule=label, component="fuel",
                                  path="fuel and line loss", qualifier="max",
                                  value=float(fm.group(1)), unit="%",
                                  effective_date=effective, source_url=source_url))
    return out


# -- Transwestern maximum rate matrix ----------------------------------------------

_TW_ROW_KIND = {"Reservation": ("reservation", "$/Dth/d"),
                "Commodity": ("usage", "$/Dth"),
                "ITS Commodity": ("usage", "$/Dth"),
                "Fuel Percentage": ("fuel", "%")}
_TW_DELIVERY = ["WOT (CAL)", "WOT (EOC)", "Ash Fork", "Phoenix", "Thoreau",
                "EOT", "San Juan (Blanco)", "I/B Link", "San Juan (N of Blanco)"]
_TW_RECEIPT = re.compile(
    r"WEST OF THOREAU|ASH FORK POINT|PHOENIX|THOREAU POINT|EAST OF THOREAU|"
    r"SAN JUAN - Blanco|I/B LINK POINT|SAN JUAN - North of Blanco")
# a receipt area's own delivery column is blank (no self-haul); rows that come
# through with one value short are missing exactly that cell
_TW_OWN_COLUMN = {"Ash Fork Point": 2, "Phoenix": 3, "Thoreau Point": 4,
                  "East Of Thoreau": 5, "San Juan - Blanco": 6,
                  "I/B Link Point": 7, "San Juan - North Of Blanco": 8}


def parse_tw(layout_pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """TW maximum transportation rate matrix (layout-mode text).

    Values are mapped to delivery-area columns by character offset against
    anchors taken from the column header line, so rows with empty cells still
    land in the right column.
    """
    out: list[TariffRate] = []
    for text in layout_pages.values():
        lines = text.splitlines()
        firm = "FTS-1"
        m = re.search(r"(FTS-\d[^\n]*?)\s*&\s*(ITS-\d)", text)
        it_sched = "ITS-1"
        if m:
            firm = m.group(1).split(",")[0].strip()
            it_sched = m.group(2)
        effective = _date(text)  # the only date on the page is the effective date
        n_cols = len(_TW_DELIVERY)
        receipt = ""
        pending: list[list[float]] = []
        for ln in lines:
            rm = _TW_RECEIPT.search(ln)
            if rm:
                receipt = rm.group(0).title()
            vals = [float(mt.group(1)) for mt in re.finditer(r"(\d+\.\d+)%?", ln)]
            if vals:
                pending.append(vals)
            # a percent row (fuel) closes the four-row receipt block:
            # Reservation / firm Commodity / ITS Commodity / Fuel
            if vals and "%" in ln:
                if len(pending) != 4 or not receipt:
                    log.warning("tw matrix: block for %r had %d rows — skipped",
                                receipt, len(pending))
                    pending, receipt = [], ""
                    continue
                own = _TW_OWN_COLUMN.get(receipt)
                kinds = ["Reservation", "Commodity", "ITS Commodity", "Fuel Percentage"]
                for row_kind, row_vals in zip(kinds, pending):
                    if len(row_vals) == n_cols:
                        cols = list(range(n_cols))
                    elif len(row_vals) == n_cols - 1 and own is not None:
                        cols = [i for i in range(n_cols) if i != own]
                    else:
                        log.warning("tw matrix: %s/%s row has %d values — skipped",
                                    receipt, row_kind, len(row_vals))
                        continue
                    comp, unit = _TW_ROW_KIND[row_kind]
                    sched = it_sched if row_kind.startswith("ITS") else \
                        ("TW" if comp == "fuel" else firm)
                    for col, val in zip(cols, row_vals):
                        out.append(TariffRate(
                            pipeline="transwestern", rate_schedule=sched,
                            component=comp, path=f"{receipt} -> {_TW_DELIVERY[col]}",
                            qualifier="" if comp == "fuel" else "max",
                            value=val, unit=unit, effective_date=effective,
                            source_url=source_url))
                pending, receipt = [], ""
    return out


# -- NGTL table of rates, tolls and charges ----------------------------------------

def parse_ngtl(layout_text: str, source_url: str = "") -> list[TariffRate]:
    """Headline NGTL tolls; per-point rates live in the attachments."""
    out: list[TariffRate] = []
    effective = ""
    em = re.search(r"Effective:\s*([A-Za-z]+ \d{1,2}, \d{4})", layout_text)
    if em:
        effective = _date(em.group(1))
    text = layout_text

    m = re.search(r"Average Firm Service Receipt Price \(AFSRP\)\s+\$([\d,.]+)", text)
    if m:
        out.append(TariffRate(pipeline="ngtl", rate_schedule="FT-R", component="demand",
                              path="AFSRP (average receipt price)", value=_money(m.group(1)),
                              unit="$/10³m³/mo", effective_date=effective,
                              source_url=source_url))
    for m in re.finditer(r"(Average )?FT-D Demand Rate for Group (\d) Delivery Points"
                         r"[^\n$]*\$([\d,.]+)", text):
        avg, grp, val = m.groups()
        out.append(TariffRate(pipeline="ngtl", rate_schedule="FT-D", component="demand",
                              path=f"Group {grp} delivery" + (" (average)" if avg else ""),
                              value=_money(val), unit="$/GJ/mo",
                              effective_date=effective, source_url=source_url))
    for label, comp_path, per in (("Monthly Abandonment Surcharge", "monthly abandonment", "mo"),
                                  ("Daily Abandonment Surcharge", "daily abandonment", "d")):
        m = re.search(re.escape(label) + r"\s+\d?\s*\$([\d,.]+)\s+/ ?10.?m3[^$]*\$([\d,.]+)\s+/ ?GJ",
                      text)
        if m:
            out.append(TariffRate(pipeline="ngtl", rate_schedule="SYSTEM",
                                  component="surcharge", path=f"{comp_path} (/10³m³)",
                                  value=_money(m.group(1)), unit=f"$/10³m³/{per}",
                                  effective_date=effective, source_url=source_url))
            out.append(TariffRate(pipeline="ngtl", rate_schedule="SYSTEM",
                                  component="surcharge", path=f"{comp_path} (/GJ)",
                                  value=_money(m.group(2)), unit=f"$/GJ/{per}",
                                  effective_date=effective, source_url=source_url))
    return out


# -- Foothills table of effective rates --------------------------------------------

def parse_foothills(text: str, source_url: str = "") -> list[TariffRate]:
    """Foothills BC/SK Table of Effective Rates: per-zone demand/commodity tolls."""
    out: list[TariffRate] = []
    effective = ""
    em = re.search(r"Effective Date:\s*(.+)", text)
    if em:
        effective = _date(em.group(1))
    schedule, component, unit = "", "", ""
    comp_map = {"Demand Rate": "demand", "Commodity Rate": "usage"}
    for ln in (s.strip() for s in text.splitlines()):
        sm = re.match(r"^\d+\.\s+Rate Schedule (\w+)\b", ln)
        if sm:
            schedule, component, unit = sm.group(1), "", ""
            continue
        am = re.match(r"^\d+\.\s+(Monthly|Daily) Abandonment Surcharge", ln)
        if am:
            schedule, component = "SYSTEM", "surcharge"
            unit = "$/GJ/mo" if am.group(1) == "Monthly" else "$/GJ/d"
            continue
        for label, comp in comp_map.items():
            if ln.startswith(label):
                component = comp
        um = re.match(r"^\(\$(.+)\)$", ln.replace(" ", ""))
        if um:
            unit = "$" + um.group(1)
            continue
        vm = re.match(r"^(Zone \d+|All Zones)\*{0,3}\s+([\d.]{6,})", ln)
        if vm and schedule and component:
            out.append(TariffRate(
                pipeline="foothills", rate_schedule=schedule, component=component,
                path=vm.group(1), qualifier="max" if component != "surcharge" else "",
                value=float(vm.group(2)), unit=unit,
                effective_date=effective, source_url=source_url))
    return out


# -- Ruby statement of rates (Kinder Morgan DART format) ---------------------------

def parse_ruby(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """Ruby Section 1 service-rate pages: long-term + short-term peak options."""
    out: list[TariffRate] = []
    comp_map = {"Monthly Reservation": ("reservation", "$/Dth/mo"),
                "Commodity Rate": ("usage", "$/Dth"),
                "Authorized Daily Overrun": ("overrun", "$/Dth")}
    for text in pages.values():
        if "STATEMENT OF RATES" not in text:
            continue
        effective = ""
        em = re.search(r"Effective on:\s*(.+)", text)
        if em:
            effective = _date(em.group(1))
        schedules: list[str] = []
        service, option, peak, component, unit = "", "", "", "", ""
        for ln in (s.strip() for s in text.splitlines()):
            if re.search(r"\bFT\s+IT\s+SS-1\s*$", ln):
                schedules = ["FT", "IT", "SS-1"]
            if "LONG-TERM SERVICE" in ln:
                service, option, peak = "long-term", "", ""
                continue
            if "SHORT-TERM SERVICE" in ln:
                service = "short-term"
                continue
            om = re.match(r"^(\d+ Month Peak Option):", ln)
            if om:
                option = om.group(1).lower()
                continue
            pm = re.match(r"^(Peak|Off-Peak) Rates:", ln)
            if pm:
                peak = pm.group(1).lower()
                continue
            for label, (comp, u) in comp_map.items():
                if ln.startswith(label):
                    component, unit = comp, u
                    break
            else:
                qm = re.match(r"^(Maximum|Minimum) Rate((?:\s+\$\s?[\d,.]+)+)", ln)
                if qm and component and schedules:
                    vals = [_money(t) for t in re.findall(r"\$\s?[\d,.]+", qm.group(2))]
                    # a lone value belongs to the firm (FT) column
                    targets = schedules if len(vals) == len(schedules) else \
                        (["FT"] if len(vals) == 1 else [])
                    path = " ".join(p for p in (service, option, peak) if p) or "base"
                    for sched, val in zip(targets, vals):
                        out.append(TariffRate(
                            pipeline="ruby", rate_schedule=sched, component=component,
                            path=path, qualifier=qm.group(1).lower()[:3], value=val,
                            unit=unit, effective_date=effective, source_url=source_url))
    return out


# -- SoCalGas Schedule G-BTS (CPUC tariff) ------------------------------------------

def parse_socal(pages: dict[int, str], source_url: str = "") -> list[TariffRate]:
    """SoCal G-BTS backbone rates: fixed reservation/volumetric rate rows.

    Only rows carrying both a reservation and a volumetric dollar value are
    emitted; market-based rows (rate caps, not fixed rates) are skipped.
    """
    out: list[TariffRate] = []
    for text in pages.values():
        if "G-BTS1" not in text:
            continue
        effective = ""
        em = re.search(r"EFFECTIVE\s+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})", text)
        if em:
            effective = _date(em.group(1))
        for ln in (s.strip() for s in text.splitlines()):
            m = re.match(r"^(G-BTS[N]?\d)\*{0,4}\s+(.*)$", ln)
            if not m or "Market" in ln:
                continue
            code, rest = m.groups()
            vals = [_money(t) for t in re.findall(r"\$[\d,.]+", rest)]
            if len(vals) != 2:
                continue
            res, vol = vals
            if "Modified" in rest:
                structure = "modified fixed variable"
            elif res and not vol:
                structure = "100% reservation"
            else:
                structure = "100% volumetric"
            for comp, val, unit in (("reservation", res, "$/Dth/d"),
                                    ("usage", vol, "$/Dth")):
                out.append(TariffRate(
                    pipeline="socal", rate_schedule=code, component=comp,
                    path=structure, qualifier="max", value=val, unit=unit,
                    effective_date=effective, source_url=source_url))
    return out


# -- post-parse validation -----------------------------------------------------------

# Per-pipeline expectations. A tariff refiling that changes the PDF layout
# should FAIL the pull (previously stored values stay in the table) rather
# than store a partial or garbled parse. require entries are
# (rate_schedule | None, component); None matches any schedule.
EXPECTATIONS: dict[str, dict] = {
    "cgt": {"min_count": 60, "require": [("G-AFT", "reservation"), ("G-CFS", "inventory")],
            "effective": True},
    "epng": {"min_count": 200, "require": [("FT-1", "reservation"), ("FUEL", "fuel")],
             "effective": True},
    "kernriver": {"min_count": 20, "require": [("KRF-1", "reservation"),
                                               ("KRF-1", "usage")], "effective": True},
    "gtn": {"min_count": 10, "require": [(None, "reservation"), (None, "fuel")],
            "effective": True},
    "nwp": {"min_count": 20, "require": [("TF-1", "reservation"), ("TI-1", "usage")],
            "effective": False},
    "transwestern": {"min_count": 300, "require": [(None, "reservation"), (None, "fuel")],
                     "effective": True},
    "ngtl": {"min_count": 5, "require": [("FT-R", "demand"), ("FT-D", "demand")],
             "effective": True},
    "foothills": {"min_count": 8, "require": [("FT", "demand"), ("IT", "usage")],
                  "effective": True},
    "ruby": {"min_count": 30, "require": [("FT", "reservation"), ("IT", "usage")],
             "effective": True},
    "socal": {"min_count": 4, "require": [("G-BTS1", "reservation")], "effective": True},
}

# sane bounds per unit family: (min, max)
_BOUNDS = {"%": (-5.0, 30.0), "$": (-1.0, 1000.0)}


def validated(pipeline: str, rates: list[TariffRate]) -> list[TariffRate]:
    """Raise if the parse looks broken (layout change); return rates unchanged."""
    exp = EXPECTATIONS.get(pipeline, {})
    problems: list[str] = []
    if len(rates) < exp.get("min_count", 1):
        problems.append(f"only {len(rates)} values parsed "
                        f"(expected >= {exp.get('min_count', 1)})")
    have = {(r.rate_schedule, r.component) for r in rates}
    have_comp = {c for _, c in have}
    for sched, comp in exp.get("require", []):
        ok = (comp in have_comp) if sched is None else ((sched, comp) in have)
        if not ok:
            problems.append(f"missing expected {sched or 'any'}/{comp}")
    if exp.get("effective") and not any(r.effective_date for r in rates):
        problems.append("no effective date parsed from the document")
    for r in rates:
        lo, hi = _BOUNDS["%"] if r.unit == "%" else _BOUNDS["$"]
        if r.value is not None and not (lo <= r.value <= hi):
            problems.append(f"out-of-range value {r.value} for "
                            f"{r.rate_schedule}/{r.component} ({r.path[:40]})")
    if problems:
        raise RuntimeError(
            f"{pipeline} rate-sheet parse failed sanity checks - layout may have "
            f"changed; keeping previously stored values. " + "; ".join(problems[:6]))
    return rates
