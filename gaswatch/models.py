"""Normalized record types shared by all pipeline adapters."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CapacityRecord:
    """Operationally available capacity / scheduled quantity at a location.

    US interstate postings (NAESB) carry design/operating/scheduled/available
    per point or segment; Canadian and intrastate systems map their
    capability/scheduled concepts onto the same columns.
    """

    pipeline: str
    gas_day: str  # YYYY-MM-DD
    cycle: str  # e.g. timely, evening, id1..id3, best_available, final
    location_type: str  # segment | point | path | area
    location_id: str
    location_name: str = ""
    design_cap: float | None = None
    operating_cap: float | None = None
    scheduled_qty: float | None = None
    available_cap: float | None = None
    unit: str = "Dth"
    flow_direction: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class FlowRecord:
    """Actual/estimated physical flow vs capability for an area or path."""

    pipeline: str
    gas_day: str
    area: str
    flow: float | None = None
    capability: float | None = None
    unit: str = "Dth"
    kind: str = "actual"  # actual | forecast | receipt | delivery
    extra: dict = field(default_factory=dict)


@dataclass
class NoticeRecord:
    """Critical/non-critical/maintenance/OFO notice."""

    pipeline: str
    notice_id: str
    category: str  # critical | non_critical | planned_outage | maintenance | ofo | tariff | other
    subject: str = ""
    body_text: str = ""
    effective_start: str = ""
    effective_end: str = ""
    posted_at: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class RateDoc:
    """A rate/tariff document or posting tracked for change detection."""

    pipeline: str
    doc_type: str  # tariff | rates | tolls | storage_rates
    title: str
    url: str
    content_hash: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class TariffRate:
    """One rate value parsed out of a tariff/rate-sheet document.

    path carries whatever context the pipeline prices by (zone-to-zone path,
    delivery area, term, season); component is the charge type normalized
    across pipelines.
    """

    pipeline: str
    rate_schedule: str  # FT-1, G-AFT, KRF-1, FTS-1 ...
    component: str  # reservation | usage | overrun | demand | fuel | total | surcharge | inventory | injection | withdrawal
    path: str = ""  # zone/path/term/season context, '' if system-wide
    qualifier: str = ""  # max | min | ''
    value: float | None = None
    unit: str = ""  # $/Dth, $/Dth/mo, $/GJ/mo, %, ...
    effective_date: str = ""  # YYYY-MM-DD when stated in the document
    source_url: str = ""
    notes: str = ""
