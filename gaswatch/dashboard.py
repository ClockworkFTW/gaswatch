"""Generate a self-contained HTML dashboard from the gaswatch database."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, datetime, timedelta

PIPELINE_LABELS = {
    "cgt": "CGT (PG&E)", "gtn": "GTN", "ngtl": "NGTL", "foothills": "Foothills",
    "epng": "EPNG", "ruby": "Ruby", "transwestern": "Transwestern", "kernriver": "Kern River",
    "nwp": "NW Pipeline", "socal": "SoCal Gas",
}

_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ",
    "%m/%d/%Y %I:%M:%S%p", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y", "%m/%d/%y %H:%M", "%m/%d/%y",
    "%m/%d/%Y %I:%M:%S %p",
)


def _parse_dt(s: str | None) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    s = " ".join(s.split())
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # e.g. "07/10/2026 8:06:48PM" (no space before AM/PM handled above); give up
    return None


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


# -- data assembly -------------------------------------------------------------


def _utilization_rows(conn: sqlite3.Connection, top_n: int = 3) -> list[dict]:
    """Latest reading for every curated key point (direct indexed lookups)."""
    out = []
    key_points = conn.execute(
        """SELECT pipeline, location_id, display_name FROM locations
           WHERE is_key_point=1 ORDER BY pipeline""").fetchall()
    for pipe, loc, name in key_points:
        row = conn.execute(
            """SELECT gas_day, cycle, operating_cap, scheduled_qty, unit FROM capacity
               WHERE pipeline=? AND location_id=? AND operating_cap > 0
                 AND scheduled_qty > 0 AND cycle != 'monthly_forecast'
               ORDER BY gas_day DESC, retrieved_at DESC LIMIT 1""",
            (pipe, loc)).fetchone()
        if not row:
            continue
        gas_day, cycle, oper, sched, unit = row
        out.append({
            "pipeline": pipe, "gas_day": gas_day, "cycle": cycle,
            "location": name, "operating": oper, "scheduled": sched,
            "unit": unit, "util": sched / oper if oper else 0,
        })
    # NGTL/Foothills physical flows vs capability (flows table)
    flow_rows = conn.execute("""
        WITH latest AS (
            SELECT pipeline, area, MAX(gas_day) AS gas_day FROM flows
            WHERE flow IS NOT NULL AND capability IS NOT NULL AND kind='actual'
            GROUP BY pipeline, area
        )
        SELECT f.pipeline, f.gas_day, f.area, f.flow, f.capability, f.unit
        FROM flows f JOIN latest l
          ON f.pipeline=l.pipeline AND f.area=l.area AND f.gas_day=l.gas_day
        WHERE f.kind='actual' AND f.capability > 0 AND f.flow IS NOT NULL
    """).fetchall()
    display = {(p, lid): name for p, lid, name in conn.execute(
        "SELECT pipeline, location_id, display_name FROM locations")}
    for r in flow_rows:
        out.append({
            "pipeline": r[0], "gas_day": r[1], "cycle": "actual flow",
            "location": display.get((r[0], r[2]), r[2]), "operating": r[4],
            "scheduled": r[3], "unit": r[5], "util": r[3] / r[4],
        })
    out.sort(key=lambda d: d["util"], reverse=True)
    return out


def _ofo_tile(conn: sqlite3.Connection, today: date, pipeline: str, label: str) -> dict:
    ofo = conn.execute("""
        SELECT COALESCE(NULLIF(effective_start, ''), posted_at), subject, extra FROM notices
        WHERE pipeline=? AND category='ofo'
        ORDER BY COALESCE(NULLIF(effective_start, ''), posted_at) DESC LIMIT 1
    """, (pipeline,)).fetchone()
    if not ofo:
        return {"label": label, "value": "None", "detail": "no events on record",
                "status": "good", "icon": "✓"}
    when, subject, extra_json = ofo
    dt = _parse_dt(when)
    active = dt and dt.date() >= today - timedelta(days=1)
    extra = json.loads(extra_json) if extra_json else {}
    stage = extra.get("stage", "")
    m = re.search(r"Stage\s*(\d)", subject or "")
    if not stage and m:
        stage = m.group(1)
    return {
        "label": label,
        "value": f"Stage {stage}" if stage != "" else "Declared",
        "detail": f"{(when or '')[:10]}" + (" — active" if active else " — last event"),
        "status": "serious" if active else "good",
        "icon": "⚠" if active else "✓",
    }


def _inventory_tile(conn: sqlite3.Connection) -> dict:
    """CGT system inventory vs its ~4,400 MMcf high-inventory OFO band."""
    row = conn.execute("""
        SELECT gas_day, flow FROM flows
        WHERE pipeline='cgt' AND area='ending_inventory' AND flow IS NOT NULL
        ORDER BY gas_day DESC LIMIT 1""").fetchone()
    if not row:
        return {"label": "CGT inventory", "value": "n/a", "detail": "", "status": None, "icon": ""}
    day, inv = row
    pct = inv / 4400 * 100
    status = "serious" if pct >= 97 else ("warning" if pct >= 92 else "good")
    return {"label": "CGT system inventory", "value": f"{inv:,.0f} MMcf",
            "detail": f"{pct:.0f}% of 4,400 OFO band ({day})",
            "status": status, "icon": "⚠" if pct >= 92 else "✓"}


def _stat_tiles(conn: sqlite3.Connection, today: date) -> list[dict]:
    tiles = [
        _ofo_tile(conn, today, "cgt", "CGT OFO"),
        _ofo_tile(conn, today, "socal", "SoCal OFO"),
        _inventory_tile(conn),
    ]
    n_maint = 0
    for start, end_s, retrieved in conn.execute(
            """SELECT effective_start, effective_end, retrieved_at FROM notices
               WHERE category IN ('maintenance', 'planned_outage')""").fetchall():
        end_dt = _parse_dt(end_s)
        if end_dt is not None and end_dt.year > 8000:
            end_dt = None  # open-ended sentinel
        if end_dt:
            if end_dt.date() >= today:
                n_maint += 1
        else:
            rdt = _parse_dt(retrieved)
            if rdt and rdt.date() >= today - timedelta(days=3):
                n_maint += 1
    tiles.append({"label": "Maintenance current/upcoming", "value": str(n_maint),
                  "detail": "all pipelines", "status": None, "icon": ""})
    return tiles


def _recent_changes(conn: sqlite3.Connection, today: date, hours: int = 48,
                    drop_pct: float = 10.0) -> list[dict]:
    """The alerts feed, stateless: what changed in the last N hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    out = []
    for pipe, cat, subject, posted in conn.execute("""
            SELECT pipeline, category, subject, posted_at FROM notices
            WHERE category IN ('critical', 'ofo') AND posted_at >= ?
            ORDER BY posted_at DESC LIMIT 40""", (cutoff[:10],)).fetchall():
        dt = _parse_dt(posted)
        if not dt or dt < datetime.now() - timedelta(hours=hours):
            continue
        out.append({"pipeline": pipe, "kind": cat.upper(), "when": posted,
                    "text": subject[:150]})
    week_ago = (today - timedelta(days=7)).isoformat()
    for pipe, doc_type, title, changed in conn.execute("""
            SELECT pipeline, doc_type, title, changed_at FROM rate_docs
            WHERE changed_at IS NOT NULL AND changed_at >= ?""", (week_ago,)).fetchall():
        out.append({"pipeline": pipe, "kind": "RATE CHANGE", "when": (changed or "")[:10],
                    "text": f"{doc_type}: {title[:130]}"})
    # capacity drops at key points: last two gas days per point, indexed lookups
    key_points = conn.execute(
        "SELECT pipeline, location_id, display_name FROM locations WHERE is_key_point=1"
    ).fetchall()
    for pipe, loc, name in key_points:
        rows = conn.execute(
            """SELECT gas_day, operating_cap FROM capacity
               WHERE pipeline=? AND location_id=? AND operating_cap > 0
                 AND scheduled_qty > 0 AND cycle != 'monthly_forecast'
               ORDER BY gas_day DESC, retrieved_at DESC LIMIT 8""",
            (pipe, loc)).fetchall()
        by_day: dict[str, float] = {}
        for day, cap in rows:
            by_day.setdefault(day, cap)
        days_sorted = sorted(by_day, reverse=True)
        if len(days_sorted) < 2:
            continue
        now_cap, prev = by_day[days_sorted[0]], by_day[days_sorted[1]]
        if now_cap < prev * (1 - drop_pct / 100.0):
            out.append({"pipeline": pipe, "kind": "CAPACITY DROP", "when": days_sorted[0],
                        "text": f"{name}: {prev:,.0f} → {now_cap:,.0f} Dth"})
    out.sort(key=lambda d: d["when"], reverse=True)
    return out


def _maintenance_rows(conn: sqlite3.Connection, today: date, limit: int = 40) -> list[dict]:
    rows = conn.execute("""
        SELECT pipeline, subject, effective_start, effective_end, retrieved_at, body_text
        FROM notices WHERE category IN ('maintenance', 'planned_outage')
    """).fetchall()
    out = []
    for pipe, subject, start_s, end_s, retrieved, body in rows:
        start_dt, end_dt = _parse_dt(start_s), _parse_dt(end_s)
        if end_dt is not None and end_dt.year > 8000:
            end_dt = None
        current = (end_dt and end_dt.date() >= today) or \
                  (end_dt is None and (_parse_dt(retrieved) or datetime.min).date()
                   >= today - timedelta(days=3))
        if not current:
            continue
        out.append({
            "pipeline": pipe, "subject": (subject or body or "")[:160],
            "start": start_s or "—", "end": end_s if end_dt else "open",
            "sort": start_dt or datetime.max,
        })
    out.sort(key=lambda d: (d["sort"], d["pipeline"]))
    return out[:limit]


def _critical_rows(conn: sqlite3.Connection, today: date, limit: int = 25) -> list[dict]:
    rows = conn.execute("""
        SELECT pipeline, category, subject, posted_at, effective_start FROM notices
        WHERE category IN ('critical', 'ofo')
    """).fetchall()
    out = []
    cutoff = today - timedelta(days=14)
    for pipe, cat, subject, posted, eff in rows:
        dt = _parse_dt(posted) or _parse_dt(eff)
        if not dt or dt.date() < cutoff:
            continue
        out.append({"pipeline": pipe, "category": cat, "subject": (subject or "")[:160],
                    "when": posted or eff, "sort": dt})
    out.sort(key=lambda d: d["sort"], reverse=True)
    return out[:limit]


def _render_rate_values(conn: sqlite3.Connection) -> str:
    """Parsed tariff rate values, one collapsible table per pipeline."""
    rows = conn.execute("""
        SELECT pipeline, rate_schedule, component, path, unit, effective_date,
               MAX(CASE WHEN qualifier IN ('max', '') THEN value END) AS vmax,
               MAX(CASE WHEN qualifier = 'min' THEN value END) AS vmin
        FROM v_current_tariff_rates
        GROUP BY pipeline, rate_schedule, component, path, unit, effective_date
        ORDER BY pipeline, rate_schedule, path, component
    """).fetchall()
    if not rows:
        return ('<p class="sub">No parsed rate values yet — run '
                '<code>gaswatch pull-all --include-heavy</code>.</p>')
    by_pipe: dict[str, list] = {}
    for r in rows:
        by_pipe.setdefault(r[0], []).append(r)
    parts = []

    def fmt(v) -> str:
        return "—" if v is None else f"{v:,.4f}".rstrip("0").rstrip(".")

    for pipe, prows in by_pipe.items():
        label = PIPELINE_LABELS.get(pipe, pipe)
        eff = max((r[5] for r in prows if r[5]), default="")
        body = [
            f'<tr><td>{_esc(r[1])}</td><td>{_esc(r[2])}</td><td>{_esc(r[3])}</td>'
            f'<td class="num">{fmt(r[6])}</td><td class="num">{fmt(r[7])}</td>'
            f'<td>{_esc(r[4])}</td><td>{_esc(r[5])}</td></tr>'
            for r in prows]
        summary = (f"<b>{_esc(label)}</b> — {len(prows)} rates"
                   + (f", newest effective {eff}" if eff else ""))
        parts.append(
            f'<details><summary>{summary}</summary>'
            f'{_render_table(["Schedule", "Component", "Path / context", "Max", "Min", "Unit", "Effective"], body)}'
            f'</details>')
    return "".join(parts)


def _render_effective_rates(conn: sqlite3.Connection, today: date) -> str:
    """Per-pipeline collapsible lists of the rate/tariff documents in effect."""
    from . import rates as ratesmod
    docs = ratesmod.effective_rate_docs(conn, today=today)
    if not docs:
        return '<p class="sub">No rate documents tracked yet.</p>'
    month_ago = (today - timedelta(days=30)).isoformat()
    by_pipe: dict[str, list[dict]] = {}
    for d in docs:
        by_pipe.setdefault(d["pipeline"], []).append(d)
    parts = []
    for pipe, rows in by_pipe.items():
        label = PIPELINE_LABELS.get(pipe, pipe)
        dated = [r["effective"] for r in rows if r["effective"] and r["status"] == "current"]
        changed = [r["changed_at"][:10] for r in rows if r["changed_at"] >= month_ago]
        summary = (f"<b>{_esc(label)}</b> — {len(rows)} "
                   f"document{'s' if len(rows) != 1 else ''}")
        if dated:
            summary += f", latest effective {max(dated)}"
        if changed:
            summary += (f' <span class="cat-rate-change">'
                        f'({len(changed)} changed since {month_ago})</span>')
        body = []
        for r in rows:
            eff = r["effective"] or "—"
            flag = ' <span class="cat-ofo">pending</span>' if r["status"] == "pending" else ""
            chg = (f' <span class="cat-rate-change">changed {r["changed_at"][:10]}</span>'
                   if r["changed_at"] >= month_ago else "")
            body.append(
                f'<tr><td>{_esc(r["doc_type"])}</td>'
                f'<td class="num">{_esc(eff)}</td>'
                f'<td><a href="{_esc(r["url"])}" target="_blank" rel="noopener">'
                f'{_esc(r["title"][:120])}</a>{flag}{chg}</td></tr>')
        parts.append(
            f'<details><summary>{summary}</summary>'
            f'{_render_table(["Type", "Effective", "Document"], body)}</details>')
    return "".join(parts)


# -- time series ---------------------------------------------------------------

# standardized panel specs: (pipeline, location_id, short series name)
PANEL_PNW = [
    ("gtn", "3500", "GTN past Kingsgate"),
    ("gtn", "1820", "GTN at Malin"),
    ("cgt", "malin", "CGT Malin receipt"),
    ("cgt", "redwood_path", "CGT Redwood"),
]
PANEL_SOCAL_BORDER = [
    ("socal", "needles_topock_area", "Needles/Topock"),
    ("socal", "el_paso_ehrenberg", "Ehrenberg (EPNG)"),
    ("socal", "kern_river_wheeler_ridge", "Wheeler Ridge (KR)"),
    ("socal", "kern_river_kramer_junction", "Kramer Jct (KR)"),
]
PANEL_CGT_PATHS = [
    ("cgt", "redwood_path", "Redwood (north)"),
    ("cgt", "topock", "Topock (south)"),
    ("cgt", "daggett", "Daggett (KR/Mojave)"),
    ("cgt", "kern_river_station", "Kern River Stn"),
]


def _util_series(conn: sqlite3.Connection, spec: list[tuple], days: int = 45) -> list[dict]:
    """Utilization % time series for a list of key points (standardized panel).

    Queries the capacity table directly on its (pipeline, location_id, gas_day)
    index — the window-function views are too slow for per-panel queries once
    the table holds hundreds of thousands of rows.
    """
    out = []
    for pipe, loc, name in spec:
        rows = conn.execute(
            """SELECT gas_day, cycle, 1.0 * scheduled_qty / operating_cap
               FROM capacity
               WHERE pipeline=? AND location_id=? AND operating_cap > 0
                 AND scheduled_qty > 0  -- skips tomorrow's zero placeholders
                 AND cycle != 'monthly_forecast' AND gas_day >= date('now', ?)
               ORDER BY gas_day, retrieved_at""",
            (pipe, loc, f"-{days} day")).fetchall()
        by_day: dict[str, float] = {}
        finals: set[str] = set()
        for day, cycle, util in rows:
            # latest retrieved wins, except the settled 'final' reading wins outright
            if cycle == "final":
                by_day[day] = util * 100
                finals.add(day)
            elif day not in finals:
                by_day[day] = util * 100
        if by_day:
            out.append({"name": name, "points": sorted(by_day.items())})
    return out


def _ngtl_flow_series(conn: sqlite3.Connection, days: int = 120) -> list[dict]:
    out = []
    for area, name in (("USJR", "Upstream James River"), ("EGAT", "East Gate"),
                       ("WGAT", "West Gate")):
        rows = conn.execute(
            """SELECT gas_day, flow FROM flows
               WHERE pipeline='ngtl' AND area=? AND kind='actual' AND flow IS NOT NULL
                 AND gas_day >= date('now', ?)
               ORDER BY gas_day""", (area, f"-{days} day")).fetchall()
        if rows:
            out.append({"name": name, "points": [(d, f) for d, f in rows]})
    return out


def _cgt_metric_series(conn: sqlite3.Connection, area: str, days: int = 120) -> list[dict]:
    rows = conn.execute(
        """SELECT gas_day, flow FROM flows
           WHERE pipeline='cgt' AND area=? AND flow IS NOT NULL
             AND gas_day >= date('now', ?)
           ORDER BY gas_day""", (area, f"-{days} day")).fetchall()
    return [{"name": area.replace("_", " "), "points": rows}] if rows else []


def _ofo_monthly(conn: sqlite3.Connection, months: int = 24) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT substr(effective_start, 1, 7) AS m, COUNT(*) FROM notices
           WHERE pipeline='cgt' AND category='ofo' AND effective_start != ''
           GROUP BY m ORDER BY m DESC LIMIT ?""", (months,)).fetchall()
    return list(reversed(rows))


def _line_chart(chart_id: str, series: list[dict], unit: str, pct: bool = False,
                w: int = 560, h: int = 210) -> str:
    """Server-rendered SVG multi-line chart with a JS crosshair+tooltip layer."""
    if not series:
        return '<p class="sub">No history yet — run backfills.</p>'
    dates = sorted({d for s in series for d, _ in s["points"]})
    if len(dates) < 2:
        return '<p class="sub">Only one day of history so far.</p>'
    ymax = max(v for s in series for _, v in s["points"]) * 1.08
    if pct:
        ymax = max(ymax, 100.0)
    left, right, top, bottom = 46, 120, 8, 20
    iw, ih = w - left - right, h - top - bottom
    x = lambda i: left + iw * i / (len(dates) - 1)
    y = lambda v: top + ih * (1 - v / ymax)
    date_idx = {d: i for i, d in enumerate(dates)}

    parts = [f'<svg viewBox="0 0 {w} {h}" class="ts-svg" role="img">']
    ticks = (0, ymax / 2, ymax)
    for tv in ticks:
        parts.append(f'<line x1="{left}" y1="{y(tv):.1f}" x2="{left + iw}" y2="{y(tv):.1f}" '
                     f'stroke="var(--grid)" stroke-width="1"/>')
        label = f"{tv:.0f}%" if pct else (f"{tv / 1000:.0f}k" if ymax >= 10000 else f"{tv:.0f}")
        parts.append(f'<text x="{left - 6}" y="{y(tv) + 4:.1f}" text-anchor="end" '
                     f'class="ts-tick">{label}</text>')
    for i in {0, len(dates) // 2, len(dates) - 1}:
        parts.append(f'<text x="{x(i):.1f}" y="{h - 4}" text-anchor="middle" '
                     f'class="ts-tick">{dates[i][5:]}</text>')
    label_ys: list[float] = []
    for si, s in enumerate(series):
        color = f"var(--s{si + 1})"
        pts = " ".join(f"{x(date_idx[d]):.1f},{y(v):.1f}" for d, v in s["points"])
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="2" stroke-linejoin="round"/>')
        # direct label at line end, nudged clear of earlier labels
        last_d, last_v = s["points"][-1]
        ly = y(last_v)
        while any(abs(ly - o) < 12 for o in label_ys):
            ly += 12
        label_ys.append(ly)
        parts.append(f'<circle cx="{x(date_idx[last_d]):.1f}" cy="{y(last_v):.1f}" r="3" '
                     f'fill="{color}"/>')
        parts.append(f'<text x="{left + iw + 8}" y="{ly + 4:.1f}" class="ts-label">'
                     f'{_esc(s["name"])}</text>')
    parts.append(f'<line class="ts-cross" x1="0" y1="{top}" x2="0" y2="{top + ih}" '
                 f'stroke="var(--baseline)" stroke-width="1" visibility="hidden"/>')
    parts.append("</svg>")

    payload = {
        "dates": dates, "unit": unit, "pct": pct,
        "left": left, "iw": iw, "w": w,
        "series": [{"name": s["name"], "color": f"var(--s{i + 1})",
                    "values": {d: v for d, v in s["points"]}}
                   for i, s in enumerate(series)],
    }
    legend = "".join(
        f'<span class="ts-key"><i style="background:var(--s{i + 1})"></i>{_esc(s["name"])}</span>'
        for i, s in enumerate(series))
    return (f'<div class="ts-wrap" id="{chart_id}" '
            f"data-chart='{html.escape(json.dumps(payload), quote=True)}'>"
            f'<div class="ts-legend">{legend}</div>{"".join(parts)}'
            f'<div class="ts-tip" hidden></div></div>')


def _bar_chart(bars: list[tuple[str, int]], w: int = 560, h: int = 170) -> str:
    if not bars:
        return '<p class="sub">No events recorded.</p>'
    left, top, bottom = 30, 8, 22
    iw, ih = w - left - 8, h - top - bottom
    ymax = max(v for _, v in bars) * 1.15
    bw = iw / len(bars)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="ts-svg" role="img">']
    for tv in (0, ymax / 2, ymax):
        yy = top + ih * (1 - tv / ymax)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{left + iw}" y2="{yy:.1f}" '
                     f'stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{left - 5}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'class="ts-tick">{tv:.0f}</text>')
    peak = max(bars, key=lambda b: b[1])
    for i, (month, count) in enumerate(bars):
        bx, bh = left + i * bw + 1, ih * count / ymax
        parts.append(
            f'<rect x="{bx:.1f}" y="{top + ih - bh:.1f}" width="{max(bw - 2, 2):.1f}" '
            f'height="{max(bh, 1):.1f}" rx="2" fill="var(--s1)">'
            f'<title>{month}: {count} OFO/EFO days</title></rect>')
        if i % 4 == 0:
            parts.append(f'<text x="{bx + bw / 2:.1f}" y="{h - 6}" text-anchor="middle" '
                         f'class="ts-tick">{month[2:7]}</text>')
        if (month, count) == peak:
            parts.append(f'<text x="{bx + bw / 2:.1f}" y="{top + ih - bh - 4:.1f}" '
                         f'text-anchor="middle" class="ts-label">{count}</text>')
    parts.append("</svg>")
    return "".join(parts)


_TS_JS = """
document.querySelectorAll('.ts-wrap').forEach(function (wrap) {
  var cfg = JSON.parse(wrap.dataset.chart);
  var svg = wrap.querySelector('svg'), tip = wrap.querySelector('.ts-tip');
  var cross = wrap.querySelector('.ts-cross');
  svg.addEventListener('mousemove', function (ev) {
    var box = svg.getBoundingClientRect();
    var fx = (ev.clientX - box.left) * (cfg.w / box.width);
    var i = Math.round((fx - cfg.left) / cfg.iw * (cfg.dates.length - 1));
    if (i < 0 || i >= cfg.dates.length) { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); return; }
    var d = cfg.dates[i];
    var sx = cfg.left + cfg.iw * i / (cfg.dates.length - 1);
    cross.setAttribute('x1', sx); cross.setAttribute('x2', sx);
    cross.setAttribute('visibility', 'visible');
    var rows = cfg.series.filter(function (s) { return s.values[d] != null; })
      .map(function (s) {
        var v = s.values[d];
        var vs = cfg.pct ? v.toFixed(0) + '%' : v.toLocaleString(undefined, {maximumFractionDigits: 0}) + ' ' + cfg.unit;
        return '<span class="ts-key"><i style="background:' + s.color + '"></i>' + s.name + ' <b>' + vs + '</b></span>';
      });
    tip.innerHTML = '<b>' + d + '</b><br>' + rows.join('<br>');
    tip.hidden = false;
    var px = (ev.clientX - box.left); var py = (ev.clientY - box.top);
    tip.style.left = Math.min(px + 14, box.width - 190) + 'px';
    tip.style.top = (py + 10) + 'px';
  });
  svg.addEventListener('mouseleave', function () {
    tip.hidden = true; cross.setAttribute('visibility', 'hidden');
  });
});
"""


def _pull_health(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT pipeline, dataset, MAX(ran_at), ok, n_records FROM pull_log
        GROUP BY pipeline, dataset ORDER BY pipeline, dataset
    """).fetchall()
    return [{"pipeline": r[0], "dataset": r[1], "ran_at": (r[2] or "").replace("T", " "),
             "ok": bool(r[3]), "n": r[4]} for r in rows]


# -- rendering -------------------------------------------------------------------

_CSS = """
:root {
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --ink-1: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series: #2a78d6;
  /* categorical slots (validated reference palette, fixed order) */
  --s1: #2a78d6; --s2: #1baf7a; --s3: #eda100; --s4: #008300;
  --status-good: #0ca30c; --status-warning: #fab219;
  --status-serious: #ec835a; --status-critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface-1: #1a1a19; --page: #0d0d0d;
    --ink-1: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --ring: rgba(255,255,255,0.10);
    --series: #3987e5;
    --s1: #3987e5; --s2: #199e70; --s3: #c98500; --s4: #008300;
  }
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--page); color: var(--ink-1);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px; }
h1 { font-size: 20px; font-weight: 650; }
h2 { font-size: 15px; font-weight: 650; margin-bottom: 10px; }
.sub { color: var(--ink-2); font-size: 13px; margin: 4px 0 20px; }
section { background: var(--surface-1); border: 1px solid var(--ring);
  border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 12px; margin-bottom: 18px; }
.tile { background: var(--surface-1); border: 1px solid var(--ring);
  border-radius: 10px; padding: 14px 16px; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 26px; font-weight: 650; margin: 2px 0; }
.tile .detail { color: var(--ink-muted); font-size: 12px; }
.tile .status-good { color: var(--status-good); }
.tile .status-serious { color: var(--status-serious); }
.bars .row { display: grid; grid-template-columns: 220px 1fr 110px;
  align-items: center; gap: 10px; padding: 3px 0; }
.bars .name { color: var(--ink-2); font-size: 12.5px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; text-align: right; }
.bars .name b { color: var(--ink-1); font-weight: 600; }
.track { position: relative; height: 14px; background:
  repeating-linear-gradient(to right, var(--grid) 0 1px, transparent 1px 25%);
  border-left: 1px solid var(--baseline); }
.fill { position: absolute; inset: 0 auto 0 0; background: var(--series);
  border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .val { font-size: 12.5px; color: var(--ink-1);
  font-variant-numeric: tabular-nums; }
.bars .val span { color: var(--ink-muted); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--ink-muted); font-weight: 550;
  border-bottom: 1px solid var(--grid); padding: 4px 10px 6px 0; }
td { border-bottom: 1px solid var(--grid); padding: 5px 10px 5px 0;
  vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.pipe { display: inline-block; font-size: 11.5px; font-weight: 600;
  color: var(--ink-2); background: color-mix(in srgb, var(--series) 12%, transparent);
  border-radius: 4px; padding: 1px 6px; white-space: nowrap; }
.ok { color: var(--status-good); } .fail { color: var(--status-critical); }
details summary { cursor: pointer; color: var(--ink-2); font-size: 13px;
  margin-top: 8px; }
.ts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 18px; }
.ts-grid section { margin-bottom: 0; }
.ts-wrap { position: relative; }
.ts-svg { width: 100%; height: auto; display: block; }
.ts-tick { font-size: 10px; fill: var(--ink-muted); }
.ts-label { font-size: 11px; fill: var(--ink-2); }
.ts-legend { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 6px; }
.ts-key { font-size: 12px; color: var(--ink-2); display: inline-flex;
  align-items: center; gap: 5px; }
.ts-key i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.ts-key b { color: var(--ink-1); font-variant-numeric: tabular-nums; }
.ts-tip { position: absolute; background: var(--surface-1); border: 1px solid var(--ring);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; pointer-events: none;
  box-shadow: 0 2px 8px rgba(0,0,0,0.12); min-width: 170px; line-height: 1.6;
  color: var(--ink-2); z-index: 5; }
.ts-tip b { color: var(--ink-1); }
a { color: var(--series); text-decoration: none; }
a:hover { text-decoration: underline; }
.cat-critical { color: var(--status-critical); font-weight: 600; }
.cat-ofo { color: var(--status-serious); font-weight: 600; }
.cat-rate-change { color: var(--series); font-weight: 600; }
.cat-capacity-drop { color: var(--status-critical); font-weight: 600; }
h2.band { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ink-muted); margin: 26px 0 12px; }
.tile .status-warning { color: var(--status-warning); }
details.foot { margin-bottom: 12px; }
details.foot section { margin-top: 8px; }
"""


def _render_utilization(rows: list[dict]) -> str:
    parts = ['<div class="bars">']
    for r in rows:
        pct = r["util"] * 100
        width = max(0.0, min(pct, 100.0))
        label = (f"{PIPELINE_LABELS.get(r['pipeline'], r['pipeline'])} · "
                 f"{r['location']}")
        tip = (f"{label} — gas day {r['gas_day']} ({r['cycle']}): scheduled "
               f"{r['scheduled']:,.0f} of {r['operating']:,.0f} {r['unit']}")
        parts.append(
            f'<div class="row" title="{_esc(tip)}">'
            f'<div class="name"><b>{_esc(PIPELINE_LABELS.get(r["pipeline"], r["pipeline"]))}</b>'
            f' {_esc(r["location"])}</div>'
            f'<div class="track"><div class="fill" style="width:{width:.1f}%"></div></div>'
            f'<div class="val">{pct:,.0f}% <span>of {r["operating"]:,.0f}</span></div>'
            f'</div>')
    parts.append("</div>")
    # accessible table view of the same data
    parts.append('<details><summary>Table view</summary><table><thead><tr>'
                 '<th>Pipeline</th><th>Location</th><th>Gas day</th><th>Cycle</th>'
                 '<th class="num">Scheduled</th><th class="num">Capacity</th>'
                 '<th class="num">Utilization</th></tr></thead><tbody>')
    for r in rows:
        parts.append(
            f'<tr><td>{_esc(PIPELINE_LABELS.get(r["pipeline"], r["pipeline"]))}</td>'
            f'<td>{_esc(r["location"])}</td><td>{_esc(r["gas_day"])}</td>'
            f'<td>{_esc(r["cycle"])}</td>'
            f'<td class="num">{r["scheduled"]:,.0f}</td>'
            f'<td class="num">{r["operating"]:,.0f} {_esc(r["unit"])}</td>'
            f'<td class="num">{r["util"] * 100:,.1f}%</td></tr>')
    parts.append("</tbody></table></details>")
    return "".join(parts)


def _render_table(headers: list[str], body_rows: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    return (f'<table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>')


def generate(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    tiles = _stat_tiles(conn, today)
    changes = _recent_changes(conn, today)
    util = _utilization_rows(conn)
    maint = _maintenance_rows(conn, today)
    health = _pull_health(conn)

    tile_html = []
    for t in tiles:
        cls = f' class="value status-{t["status"]}"' if t["status"] else ' class="value"'
        icon = f'{t["icon"]} ' if t["icon"] else ""
        tile_html.append(
            f'<div class="tile"><div class="label">{_esc(t["label"])}</div>'
            f'<div{cls}>{icon}{_esc(t["value"])}</div>'
            f'<div class="detail">{_esc(t["detail"])}</div></div>')

    maint_rows = [
        f'<tr><td><span class="pipe">{_esc(PIPELINE_LABELS.get(m["pipeline"], m["pipeline"]))}</span></td>'
        f'<td>{_esc(m["start"])}</td><td>{_esc(m["end"])}</td><td>{_esc(m["subject"])}</td></tr>'
        for m in maint]
    change_rows = [
        f'<tr><td><span class="pipe">{_esc(PIPELINE_LABELS.get(c["pipeline"], c["pipeline"]))}</span></td>'
        f'<td class="cat-{_esc(c["kind"].lower().replace(" ", "-"))}">{_esc(c["kind"])}</td>'
        f'<td>{_esc(c["when"])}</td><td>{_esc(c["text"])}</td></tr>'
        for c in changes]
    health_rows = [
        f'<tr><td>{_esc(PIPELINE_LABELS.get(h["pipeline"], h["pipeline"]))}</td>'
        f'<td>{_esc(h["dataset"])}</td><td>{_esc(h["ran_at"])}</td>'
        f'<td class="{"ok" if h["ok"] else "fail"}">{"ok" if h["ok"] else "FAILED"}</td>'
        f'<td class="num">{h["n"]}</td></tr>'
        for h in health]

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gaswatch — western pipelines</title><style>{_CSS}</style></head>
<body>
<h1>Western gas pipelines — operations dashboard</h1>
<p class="sub">Generated {generated}. Ten systems: CGT · SoCal · GTN · NWP · NGTL ·
Foothills · EPNG · Transwestern · Kern River · Ruby.</p>

<h2 class="band">Briefing — system stress &amp; what changed</h2>
<div class="tiles">{''.join(tile_html)}</div>
<section><h2>Changes — last 48 hours</h2>
{_render_table(["Pipeline", "Type", "When", "What"], change_rows)
 if change_rows else '<p class="sub">Nothing new in the window.</p>'}</section>
<section><h2>Maintenance &amp; planned outages — current and upcoming</h2>
{_render_table(["Pipeline", "Start", "End", "Item"], maint_rows)
 if maint_rows else '<p class="sub">No current maintenance postings.</p>'}</section>

<h2 class="band">Monitor — corridors &amp; trends</h2>
<div class="ts-grid">
<section><h2>PNW corridor: Kingsgate → Malin (utilization %)</h2>
{_line_chart('ts-pnw', _util_series(conn, PANEL_PNW), '', pct=True)}</section>
<section><h2>SoCal border receipts (utilization %)</h2>
{_line_chart('ts-socal', _util_series(conn, PANEL_SOCAL_BORDER), '', pct=True)}</section>
<section><h2>CGT paths (utilization %)</h2>
{_line_chart('ts-cgt', _util_series(conn, PANEL_CGT_PATHS), '', pct=True)}</section>
<section><h2>NGTL area flows (10³m³/d)</h2>
{_line_chart('ts-ngtl', _ngtl_flow_series(conn), '10³m³/d')}</section>
<section><h2>CGT system inventory (MMcf)</h2>
{_line_chart('ts-inv', _cgt_metric_series(conn, 'ending_inventory'), 'MMcf')}</section>
<section><h2>CGT OFO/EFO days per month — last 24 months</h2>
{_bar_chart(_ofo_monthly(conn))}</section>
</div>

<h2 class="band">Reference — currently effective rates</h2>
<section><h2>Transportation &amp; storage rates (parsed values)</h2>
<p class="sub">Reservation, usage/commodity, fuel and surcharge values parsed from each
pipeline's statement of rates / rate matrix. Refresh with
<code>gaswatch pull-all --include-heavy</code> (or per pipeline:
<code>gaswatch pull -p epng -d rate_values</code>).</p>
{_render_rate_values(conn)}</section>
<section><h2>Rate &amp; tariff documents in effect (parsed from EBB postings)</h2>
<p class="sub">Per series (rate matrix, monthly index rates, …) only the posting in
effect today is shown; undated entries are standing tariff documents. Links open the
pipeline's own posting.</p>
{_render_effective_rates(conn, today)}</section>

<details class="foot"><summary>All key points, latest gas day</summary>
<section>{_render_utilization(util)}</section></details>
<details class="foot"><summary>Pull health</summary>
<section>{_render_table(["Pipeline", "Dataset", "Last run", "Status", "Records"], health_rows)}
</section></details>
<script>{_TS_JS}</script>
</body></html>"""
