"""SQLite storage: schema + idempotent upserts."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import CapacityRecord, FlowRecord, NoticeRecord, RateDoc, TariffRate

DEFAULT_DB = Path(os.environ.get("GASWATCH_DB", "data/gaswatch.db"))

# -- v_throughput unit normalization -------------------------------------------
# ~1030 Btu/cf heat content: 1 MMcf of pipeline gas ~ 1030 MMBtu (= 1030 Dth).
# 1 e3m3 (10^3 m3) = 35.3147 Mcf. Approximations fit for corridor reasoning,
# not invoice math.
MMBTU_PER_MMCF = 1030.0
MCF_PER_E3M3 = 35.3147

# source unit -> factor to MMcf/d. v_throughput's CASE is generated from this
# map, and export-powerbi refuses to run if a unit outside it ever appears
# (a unit missing here would otherwise export silent NULLs).
UNIT_TO_MMCFD = {
    "Dth":    1.0 / MMBTU_PER_MMCF,
    "MMBtu":  1.0 / MMBTU_PER_MMCF,
    "MMcf":   1.0,
    "e3m3/d": MCF_PER_E3M3 / 1000.0,
    "e6m3/d": MCF_PER_E3M3,
}

# flows kinds that are throughput-shaped (vs storage/demand/temperature series)
THROUGHPUT_KINDS = ("actual", "cer_throughput", "snapshot", "receipt",
                    "scheduled", "estimate", "forecast")

# Areas that ride in on a throughput kind but are NOT point/border flows.
# SoCal's daily-operations report posts storage, demand, imbalance, fuel and
# temperature under kind actual/estimate/forecast (its "Deliveries" and
# "Balancing" sections, plus the total_receipts aggregate); NGTL's snapshot
# includes linepack (inventory). These belong in system_metrics, not
# v_throughput. Blocklist, so a NEW aggregate label on one of these reports
# must be added here or it will leak into the throughput fact.
NON_THROUGHPUT_AREAS = {
    "socal": (
        "composite_weighted_average_temperature_f",
        "cumulative_customer_imbalance",
        "ending_storage_balance_mcf",
        "injection_capacity",
        "net_injections_withdrawals",
        "storage_injection_for_customer_balancing_withdrawal",
        "system_sendout",
        "total_daily_customer_imbalance",
        "total_deliveries",
        "total_receipts",
        "transmission_fuel_use",
        "withdrawal_capacity",
    ),
    "ngtl": ("LINEPACK",),
}


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# WHERE predicate selecting the throughput-shaped subset of `flows`. Shared by
# v_throughput (below) and export-powerbi's system_metrics query (negated
# there), so the two partitions cannot drift apart.
FLOWS_THROUGHPUT_PREDICATE = (
    f"kind IN {_sql_list(THROUGHPUT_KINDS)}"
    + "".join(
        f"\n              AND NOT (pipeline = '{p}' AND area IN {_sql_list(areas)})"
        for p, areas in NON_THROUGHPUT_AREAS.items()
    )
)

_UNIT_CASE = ("CASE unit_src "
              + " ".join(f"WHEN '{u}' THEN {f!r}" for u, f in UNIT_TO_MMCFD.items())
              + " END")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS capacity (
    pipeline TEXT NOT NULL,
    gas_day TEXT NOT NULL,
    cycle TEXT NOT NULL,
    location_type TEXT NOT NULL,
    location_id TEXT NOT NULL,
    location_name TEXT,
    design_cap REAL,
    operating_cap REAL,
    scheduled_qty REAL,
    available_cap REAL,
    unit TEXT,
    flow_direction TEXT NOT NULL DEFAULT '',
    extra TEXT,
    retrieved_at TEXT NOT NULL,
    -- location_id alone is NOT unique on some systems (EPNG reuses segment
    -- numbers across named points and directions), so identity includes
    -- name and direction
    UNIQUE (pipeline, gas_day, cycle, location_type, location_id,
            location_name, flow_direction)
);
CREATE TABLE IF NOT EXISTS flows (
    pipeline TEXT NOT NULL,
    gas_day TEXT NOT NULL,
    area TEXT NOT NULL,
    kind TEXT NOT NULL,
    flow REAL,
    capability REAL,
    unit TEXT,
    extra TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (pipeline, gas_day, area, kind)
);
CREATE TABLE IF NOT EXISTS notices (
    pipeline TEXT NOT NULL,
    notice_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT,
    body_text TEXT,
    effective_start TEXT,
    effective_end TEXT,
    posted_at TEXT,
    url TEXT,
    extra TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (pipeline, notice_id, category)
);
CREATE TABLE IF NOT EXISTS rate_docs (
    pipeline TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    content_hash TEXT,
    extra TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    changed_at TEXT,
    UNIQUE (pipeline, doc_type, url)
);
CREATE TABLE IF NOT EXISTS tariff_rates (
    pipeline TEXT NOT NULL,
    rate_schedule TEXT NOT NULL,
    component TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    qualifier TEXT NOT NULL DEFAULT '',
    value REAL,
    unit TEXT NOT NULL DEFAULT '',
    effective_date TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    notes TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (pipeline, rate_schedule, component, path, qualifier, effective_date)
);
CREATE TABLE IF NOT EXISTS raw_fetches (
    pipeline TEXT NOT NULL,
    dataset TEXT NOT NULL,
    url TEXT NOT NULL,
    params TEXT,
    status INTEGER,
    saved_path TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pull_log (
    pipeline TEXT NOT NULL,
    dataset TEXT NOT NULL,
    ok INTEGER NOT NULL,
    n_records INTEGER,
    message TEXT,
    ran_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS locations (
    pipeline TEXT NOT NULL,
    location_id TEXT NOT NULL,
    location_type TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    is_key_point INTEGER NOT NULL DEFAULT 0,
    interconnect_group TEXT,      -- same value = physically connected points
    notes TEXT,
    UNIQUE (pipeline, location_id)
);
CREATE INDEX IF NOT EXISTS idx_capacity_pipe_day ON capacity (pipeline, gas_day);
CREATE INDEX IF NOT EXISTS idx_capacity_pipe_loc_day ON capacity (pipeline, location_id, gas_day);
CREATE INDEX IF NOT EXISTS idx_flows_pipe_area_day ON flows (pipeline, area, gas_day);
CREATE INDEX IF NOT EXISTS idx_notices_cat_posted ON notices (category, posted_at);

-- latest reading per location per gas day (most recent cycle wins)
CREATE VIEW IF NOT EXISTS v_capacity_latest AS
SELECT pipeline, gas_day, cycle, location_type, location_id, location_name,
       design_cap, operating_cap, scheduled_qty, available_cap, unit,
       flow_direction, retrieved_at
FROM (
    SELECT c.*, ROW_NUMBER() OVER (
        PARTITION BY pipeline, gas_day, location_type, location_id,
                     location_name, flow_direction
        ORDER BY retrieved_at DESC) AS rn
    FROM capacity c WHERE cycle != 'monthly_forecast'
) WHERE rn = 1;

-- newest effective rate per (pipeline, schedule, component, path, qualifier)
-- that is already in force; older filings remain as history
CREATE VIEW IF NOT EXISTS v_current_tariff_rates AS
SELECT pipeline, rate_schedule, component, path, qualifier, value, unit,
       effective_date, source_url, notes, retrieved_at
FROM (
    SELECT t.*, ROW_NUMBER() OVER (
        PARTITION BY pipeline, rate_schedule, component, path, qualifier
        ORDER BY effective_date DESC) AS rn
    FROM tariff_rates t
    WHERE effective_date <= date('now') OR effective_date = ''
) WHERE rn = 1;

-- utilization joined to the curated locations dimension
CREATE VIEW IF NOT EXISTS v_utilization AS
SELECT v.pipeline, v.gas_day, v.cycle, v.location_type, v.location_id,
       COALESCE(l.display_name, v.location_name, v.location_id) AS display_name,
       v.scheduled_qty, v.operating_cap, v.available_cap, v.unit,
       CASE WHEN v.operating_cap > 0
            THEN 1.0 * v.scheduled_qty / v.operating_cap END AS utilization,
       COALESCE(l.is_key_point, 0) AS is_key_point,
       l.interconnect_group
FROM v_capacity_latest v
LEFT JOIN locations l
  ON l.pipeline = v.pipeline AND l.location_id = v.location_id
WHERE v.operating_cap IS NOT NULL;

-- Normalized throughput: every pipeline's flow/capacity in ONE table, in MMcf/d.
-- Conversion is driven by the source `unit` column via UNIT_TO_MMCFD (defined
-- above; the CASE below is generated from it). Two sources are unioned: the
-- daily capacity postings (every US pipeline, Dth) and the throughput-shaped
-- `flows` subset (FLOWS_THROUGHPUT_PREDICATE, also defined above). NGTL and
-- Foothills post only monthly capability forecasts to `capacity` (dropped by
-- v_capacity_latest), so their real per-day data arrives via flows -- this view
-- is what puts them on the same footing as the Dth pipelines. Non-throughput
-- series (storage, demand, inventory, imbalance, supply, temperature) are
-- excluded by kind, and by area for the pipelines that mix metric rows into a
-- throughput kind (SoCal, NGTL linepack).
-- The flows side keeps ONE row per (pipeline, gas_day, area): SoCal reposts a
-- gas day as forecast, then estimate, then actual, and the best available kind
-- wins (actual > estimate > forecast; other kinds use disjoint area names).
-- dropped+recreated (not IF NOT EXISTS) so edits to this definition reach
-- databases that already have it; a view holds no data, so this is free.
DROP VIEW IF EXISTS v_throughput;
CREATE VIEW v_throughput AS
SELECT pipeline, gas_day, kind, source, location_type, location_id, location_name,
       flow_direction, unit_src,
       scheduled_raw * f AS scheduled_mmcfd,
       capacity_raw  * f AS capacity_mmcfd,
       design_raw    * f AS design_mmcfd,
       available_raw * f AS available_mmcfd,
       CASE WHEN capacity_raw > 0
            THEN 1.0 * scheduled_raw / capacity_raw END AS utilization
FROM (
    SELECT s.*, {_UNIT_CASE} AS f
    FROM (
        SELECT pipeline, gas_day, cycle AS kind, 'capacity' AS source,
               location_type, location_id,
               COALESCE(NULLIF(location_name, ''), location_id) AS location_name,
               COALESCE(flow_direction, '') AS flow_direction,
               unit AS unit_src, scheduled_qty AS scheduled_raw,
               operating_cap AS capacity_raw, design_cap AS design_raw,
               available_cap AS available_raw
        FROM v_capacity_latest
        UNION ALL
        SELECT pipeline, gas_day, kind, 'flows', 'area', area, area, '',
               unit, flow, capability, NULL, NULL
        FROM (
            SELECT fl.*, ROW_NUMBER() OVER (
                PARTITION BY pipeline, gas_day, area
                ORDER BY CASE kind WHEN 'actual'   THEN 0
                                   WHEN 'estimate' THEN 1
                                   WHEN 'forecast' THEN 2 ELSE 3 END, kind) AS rn
            FROM flows fl
            WHERE {FLOWS_THROUGHPUT_PREDICATE}
        ) WHERE rn = 1
    ) s
);
"""

# Curated key points (edit freely; re-seeded idempotently on connect).
# interconnect_group ties physically-connected points across pipelines:
# Foothills BC -> Kingsgate -> GTN -> Malin -> CGT Redwood; EPNG -> Topock -> CGT Baja.
KEY_LOCATIONS = [
    # (pipeline, location_id, location_type, display_name, group)
    ("gtn", "3498", "point", "Kingsgate (BC border receipt)", "kingsgate"),
    ("gtn", "3500", "segment", "Flow Past Kingsgate", "kingsgate"),
    ("gtn", "1820", "point", "Malin (delivery to CGT)", "malin"),
    ("gtn", "28218", "segment", "Station 8 CFTP", None),
    ("cgt", "redwood_path", "path", "Redwood Path", None),
    ("cgt", "malin", "path", "Malin (receipt from GTN)", "malin"),
    ("cgt", "onyx_hill", "path", "Onyx Hill (receipt from Ruby)", "ruby_delivery"),
    ("cgt", "topock", "path", "Topock (receipt from EPNG/Transwestern)", "topock"),
    ("cgt", "kern_river_station", "path", "Kern River Station (SoCal)", None),
    ("epng", "160", "segment", "San Juan Triangle", None),
    ("epng", "320", "segment", "Topock (CA border)", "topock"),
    ("epng", "300", "segment", "Hackberry", None),
    ("ruby", "10", "segment", "Opal Hub receipt", None),
    ("ruby", "60", "segment", "Tule Lake (delivery toward Malin)", "ruby_delivery"),
    ("ngtl", "USJR", "area", "Upstream James River", None),
    ("ngtl", "EGAT", "area", "East Gate", None),
    ("ngtl", "WGAT", "area", "West Gate", None),
    ("ngtl", "ALBERTA_BORDER", "area", "AB/BC border (to Foothills BC)", "kingsgate"),
    ("foothills", "ALBERTA_BORDER", "area", "AB/BC border flow (toward Kingsgate)", "kingsgate"),
    ("foothills", "MCNEIL_BORDER", "area", "McNeill (SK border)", None),
    ("foothills", "EMPRESS_BORDER", "area", "Empress", None),
    ("transwestern", "10487", "point", "SoCal Needles (CA border)", "needles"),
    ("transwestern", "78484", "point", "Red Hawk Plant", None),
    ("kernriver", "024011", "segment", "Daggett - PG&E (to CGT)", "daggett"),
    ("kernriver", "025011", "segment", "Wheeler Ridge - SoCal Gas", "wheeler_ridge"),
    ("kernriver", "054016", "segment", "Daggett Compressor", "daggett"),
    ("kernriver", "054001", "segment", "Muddy Creek Compressor (Opal receipt)", None),
    ("cgt", "daggett", "path", "Daggett (receipt from Kern River/Mojave)", "daggett"),
    ("nwp", "28219", "point", "Sumas receipt (BC border)", None),
    ("nwp", "236124", "point", "Jackson Prairie", None),
    ("nwp", "28164", "point", "Mt. Vernon compressor", None),
    ("socal", "needles_topock_area", "point", "Needles/Topock area receipts", "topock"),
    ("socal", "el_paso_ehrenberg", "point", "El Paso - Ehrenberg", "ehrenberg"),
    ("socal", "transwestern_needles", "point", "Transwestern - Needles", "needles"),
    ("socal", "kern_river_wheeler_ridge", "point", "Kern River - Wheeler Ridge", "wheeler_ridge"),
    ("socal", "kern_river_kramer_junction", "point", "Kern River - Kramer Junction", "kramer"),
    ("kernriver", "025032", "segment", "Kramer Junction - SoCal Gas", "kramer"),
    # CGT interconnect receipts (flows `receipt` areas) -- the actual volumes
    # arriving at the California border from each upstream system.
    ("cgt", "gas_transmission_northwest", "point", "CGT receipt from GTN (Malin)", "malin"),
    ("cgt", "ruby", "point", "CGT receipt from Ruby", "ruby_delivery"),
    ("cgt", "el_paso_natural_gas", "point", "CGT receipt from EPNG (Topock)", "topock"),
    ("cgt", "transwestern", "point", "CGT receipt from Transwestern (Topock)", "topock"),
    ("cgt", "kern_river_gt_daggett", "point", "CGT receipt from Kern River (Daggett)", "daggett"),
    ("cgt", "kern_river_gt_hdl", "point", "CGT receipt from Kern River (HDL)", "daggett"),
    ("cgt", "california_production", "point", "California in-state production", None),
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO locations (pipeline, location_id, location_type, display_name,
               is_key_point, interconnect_group) VALUES (?,?,?,?,1,?)
           ON CONFLICT (pipeline, location_id) DO UPDATE SET
               location_type=excluded.location_type, display_name=excluded.display_name,
               is_key_point=1, interconnect_group=excluded.interconnect_group""",
        KEY_LOCATIONS,
    )
    conn.commit()
    return conn


def unknown_throughput_units(conn: sqlite3.Connection) -> list[str]:
    """Units feeding v_throughput that have no factor in UNIT_TO_MMCFD.

    The view's generated CASE yields NULL for such rows, which would export as
    silently-empty MMcf/d values -- callers should fail loudly instead."""
    known = _sql_list(UNIT_TO_MMCFD)
    rows = conn.execute(f"""
        SELECT DISTINCT unit FROM capacity
        WHERE cycle != 'monthly_forecast'
          AND unit IS NOT NULL AND unit != '' AND unit NOT IN {known}
        UNION
        SELECT DISTINCT unit FROM flows
        WHERE {FLOWS_THROUGHPUT_PREDICATE}
          AND unit IS NOT NULL AND unit != '' AND unit NOT IN {known}
    """).fetchall()
    return sorted(r[0] for r in rows)


def _extra(rec) -> str:
    return json.dumps(rec.extra, default=str) if rec.extra else ""


def upsert_capacity(conn: sqlite3.Connection, records: list[CapacityRecord]) -> int:
    ts = now_utc()
    conn.executemany(
        """INSERT INTO capacity (pipeline, gas_day, cycle, location_type, location_id,
               location_name, design_cap, operating_cap, scheduled_qty, available_cap,
               unit, flow_direction, extra, retrieved_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (pipeline, gas_day, cycle, location_type, location_id,
                        location_name, flow_direction) DO UPDATE SET
               design_cap=excluded.design_cap,
               operating_cap=excluded.operating_cap, scheduled_qty=excluded.scheduled_qty,
               available_cap=excluded.available_cap, unit=excluded.unit,
               extra=excluded.extra,
               retrieved_at=excluded.retrieved_at""",
        [
            (r.pipeline, r.gas_day, r.cycle, r.location_type, r.location_id,
             r.location_name, r.design_cap, r.operating_cap, r.scheduled_qty,
             r.available_cap, r.unit, r.flow_direction, _extra(r), ts)
            for r in records
        ],
    )
    conn.commit()
    return len(records)


def upsert_flows(conn: sqlite3.Connection, records: list[FlowRecord]) -> int:
    ts = now_utc()
    conn.executemany(
        """INSERT INTO flows (pipeline, gas_day, area, kind, flow, capability, unit, extra, retrieved_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (pipeline, gas_day, area, kind) DO UPDATE SET
               flow=excluded.flow, capability=excluded.capability, unit=excluded.unit,
               extra=excluded.extra, retrieved_at=excluded.retrieved_at""",
        [
            (r.pipeline, r.gas_day, r.area, r.kind, r.flow, r.capability, r.unit, _extra(r), ts)
            for r in records
        ],
    )
    conn.commit()
    return len(records)


def upsert_notices(conn: sqlite3.Connection, records: list[NoticeRecord]) -> int:
    ts = now_utc()
    conn.executemany(
        """INSERT INTO notices (pipeline, notice_id, category, subject, body_text,
               effective_start, effective_end, posted_at, url, extra, retrieved_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (pipeline, notice_id, category) DO UPDATE SET
               subject=excluded.subject, body_text=excluded.body_text,
               effective_start=excluded.effective_start, effective_end=excluded.effective_end,
               posted_at=excluded.posted_at, url=excluded.url, extra=excluded.extra,
               retrieved_at=excluded.retrieved_at""",
        [
            (r.pipeline, r.notice_id, r.category, r.subject, r.body_text,
             r.effective_start, r.effective_end, r.posted_at, r.url, _extra(r), ts)
            for r in records
        ],
    )
    conn.commit()
    return len(records)


def upsert_tariff_rates(conn: sqlite3.Connection, records: list[TariffRate]) -> int:
    ts = now_utc()
    conn.executemany(
        """INSERT INTO tariff_rates (pipeline, rate_schedule, component, path,
               qualifier, value, unit, effective_date, source_url, notes, retrieved_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (pipeline, rate_schedule, component, path, qualifier, effective_date)
           DO UPDATE SET value=excluded.value, unit=excluded.unit,
               source_url=excluded.source_url, notes=excluded.notes,
               retrieved_at=excluded.retrieved_at""",
        [
            (r.pipeline, r.rate_schedule, r.component, r.path, r.qualifier,
             r.value, r.unit, r.effective_date, r.source_url, r.notes, ts)
            for r in records
        ],
    )
    conn.commit()
    return len(records)


def upsert_rate_docs(conn: sqlite3.Connection, records: list[RateDoc]) -> list[RateDoc]:
    """Upsert rate docs; returns the subset whose content hash changed (or is new)."""
    ts = now_utc()
    changed: list[RateDoc] = []
    for r in records:
        row = conn.execute(
            "SELECT content_hash FROM rate_docs WHERE pipeline=? AND doc_type=? AND url=?",
            (r.pipeline, r.doc_type, r.url),
        ).fetchone()
        if row is None:
            # newly tracked, not "changed": changed_at stays NULL until the
            # content hash actually moves
            conn.execute(
                """INSERT INTO rate_docs (pipeline, doc_type, title, url, content_hash,
                       extra, first_seen, last_seen, changed_at)
                   VALUES (?,?,?,?,?,?,?,?,NULL)""",
                (r.pipeline, r.doc_type, r.title, r.url, r.content_hash, _extra(r), ts, ts),
            )
            changed.append(r)
        elif row[0] != r.content_hash and r.content_hash:
            conn.execute(
                """UPDATE rate_docs SET title=?, content_hash=?, extra=?, last_seen=?, changed_at=?
                   WHERE pipeline=? AND doc_type=? AND url=?""",
                (r.title, r.content_hash, _extra(r), ts, ts, r.pipeline, r.doc_type, r.url),
            )
            changed.append(r)
        else:
            conn.execute(
                "UPDATE rate_docs SET last_seen=? WHERE pipeline=? AND doc_type=? AND url=?",
                (ts, r.pipeline, r.doc_type, r.url),
            )
    conn.commit()
    return changed


def notices_missing_body(conn: sqlite3.Connection, pipeline: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT notice_id, category, url, extra FROM notices
           WHERE pipeline=? AND (body_text IS NULL OR body_text='')
           ORDER BY posted_at DESC LIMIT ?""",
        (pipeline, limit),
    ).fetchall()
    return [{"notice_id": r[0], "category": r[1], "url": r[2],
             "extra": json.loads(r[3]) if r[3] else {}} for r in rows]


def update_notice_bodies(conn: sqlite3.Connection, pipeline: str, bodies: dict[str, str]) -> int:
    for notice_id, body in bodies.items():
        conn.execute(
            "UPDATE notices SET body_text=? WHERE pipeline=? AND notice_id=? "
            "AND (body_text IS NULL OR body_text='')",
            (body, pipeline, notice_id),
        )
    conn.commit()
    return len(bodies)


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                 "ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def log_raw_fetch(conn: sqlite3.Connection, pipeline: str, dataset: str, url: str,
                  params: dict | None, status: int, saved_path: str) -> None:
    conn.execute(
        "INSERT INTO raw_fetches (pipeline, dataset, url, params, status, saved_path, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (pipeline, dataset, url, json.dumps(params or {}, default=str), status, saved_path, now_utc()),
    )
    conn.commit()


def log_pull(conn: sqlite3.Connection, pipeline: str, dataset: str, ok: bool,
             n_records: int, message: str = "") -> None:
    conn.execute(
        "INSERT INTO pull_log (pipeline, dataset, ok, n_records, message, ran_at) VALUES (?,?,?,?,?,?)",
        (pipeline, dataset, 1 if ok else 0, n_records, message, now_utc()),
    )
    conn.commit()
