"""gaswatch CLI: pull EBB data into SQLite."""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer

from . import db as dbm
from .http import EbbClient
from .pipelines import get_adapter, pipeline_names
from .pipelines.base import PipelineAdapter

app = typer.Typer(help="Western gas pipeline EBB scraper — ten systems: CGT, SoCal, "
                       "GTN, NWP, NGTL, Foothills, EPNG, Transwestern, Kern River, Ruby")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("gaswatch")


def _parse_day(value: str | None) -> date:
    return date.today() if value is None else datetime.strptime(value, "%Y-%m-%d").date()


def _store(conn, result) -> int:
    n = 0
    n += dbm.upsert_capacity(conn, result.capacity)
    n += dbm.upsert_flows(conn, result.flows)
    n += dbm.upsert_notices(conn, result.notices)
    n += dbm.upsert_tariff_rates(conn, result.rate_values)
    changed = dbm.upsert_rate_docs(conn, result.rate_docs)
    n += len(result.rate_docs)
    for doc in changed:
        log.info("RATE DOC NEW/CHANGED: [%s] %s -> %s", doc.pipeline, doc.title, doc.url)
    return n


def _pull_one(conn, client, pipeline: str, dataset: str, gas_day: date,
              start: date | None = None, end: date | None = None) -> bool:
    adapter = get_adapter(pipeline)
    try:
        result = adapter.fetch(client, dataset, gas_day, start=start, end=end)
    except Exception as exc:  # fail loudly per-dataset, keep the run going
        log.error("%s/%s FAILED: %s", pipeline, dataset, exc)
        dbm.log_pull(conn, pipeline, dataset, ok=False, n_records=0, message=str(exc)[:500])
        return False
    n = _store(conn, result)
    for path in result.raw_paths:
        dbm.log_raw_fetch(conn, pipeline, dataset, "", None, 200, path)
    dbm.log_pull(conn, pipeline, dataset, ok=True, n_records=n)
    log.info("%s/%s: %d records", pipeline, dataset, n)
    if result.notices and \
            type(adapter).fetch_notice_bodies is not PipelineAdapter.fetch_notice_bodies:
        _enrich_notice_bodies(conn, client, adapter)
    return True


def _enrich_notice_bodies(conn, client, adapter) -> None:
    """Fetch body text for stored notices that don't have one yet."""
    missing = dbm.notices_missing_body(conn, adapter.name)
    if not missing:
        return
    try:
        bodies = adapter.fetch_notice_bodies(client, missing)
    except Exception as exc:
        log.error("%s notice-body fetch failed: %s", adapter.name, exc)
        return
    if bodies:
        dbm.update_notice_bodies(conn, adapter.name, bodies)
        log.info("%s: filled body text for %d notices", adapter.name, len(bodies))


@app.command("init-db")
def init_db(db: Path = typer.Option(dbm.DEFAULT_DB, help="SQLite database path")):
    """Create the database and schema."""
    conn = dbm.connect(db)
    conn.close()
    typer.echo(f"initialized {db}")


@app.command()
def pipelines():
    """List pipelines and their datasets."""
    for name in pipeline_names():
        try:
            adapter = get_adapter(name)
            typer.echo(f"{name}: {', '.join(adapter.datasets())}")
        except Exception as exc:
            typer.echo(f"{name}: unavailable ({exc})")


@app.command()
def pull(
    pipeline: str = typer.Option(..., "--pipeline", "-p"),
    dataset: str = typer.Option(..., "--dataset", "-d"),
    gas_day: str = typer.Option(None, "--gas-day", help="YYYY-MM-DD (default today)"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Pull one dataset for one pipeline."""
    conn = dbm.connect(db)
    client = EbbClient()
    try:
        ok = _pull_one(conn, client, pipeline.lower(), dataset, _parse_day(gas_day))
    finally:
        client.close()
        conn.close()
    raise typer.Exit(0 if ok else 1)


def _pull_all(conn, client, day: date, include_browser: bool, include_heavy: bool) -> int:
    """Pull every current-day dataset for every pipeline. Returns failure count."""
    failures = 0
    for name in pipeline_names():
        try:
            adapter = get_adapter(name)
        except Exception as exc:
            log.error("%s unavailable: %s", name, exc)
            failures += 1
            continue
        for ds in adapter.datasets():
            if ds in adapter.BROWSER_DATASETS and not include_browser:
                log.info("%s/%s skipped (browser-based; use --include-browser)", name, ds)
                continue
            if ds in adapter.HEAVY_DATASETS and not include_heavy:
                log.info("%s/%s skipped (large download; use --include-heavy)", name, ds)
                continue
            if not _pull_one(conn, client, name, ds, day):
                failures += 1
    return failures


def _backfill_all(conn, client, d0: date, d1: date) -> tuple[int, int]:
    """Backfill [d0, d1] for every pipeline's BACKFILL_DATASETS.

    Returns (datasets_attempted, failures)."""
    attempted = failures = 0
    for name in pipeline_names():
        try:
            adapter = get_adapter(name)
        except Exception as exc:
            log.error("%s unavailable: %s", name, exc)
            continue
        for ds in adapter.BACKFILL_DATASETS:
            attempted += 1
            log.info("backfilling %s/%s %s..%s", name, ds, d0, d1)
            if not _pull_one(conn, client, name, ds, d1, start=d0, end=d1):
                failures += 1
    return attempted, failures


@app.command("pull-all")
def pull_all(
    gas_day: str = typer.Option(None, "--gas-day", help="YYYY-MM-DD (default today)"),
    include_browser: bool = typer.Option(False, "--include-browser",
                                         help="Also run Playwright-based Ruby capacity pull"),
    include_heavy: bool = typer.Option(False, "--include-heavy",
                                       help="Also run multi-MB tariff-PDF rate-value pulls "
                                            "(schedule weekly — tariff rates change rarely)"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Pull every dataset for every pipeline for one gas day."""
    day = _parse_day(gas_day)
    conn = dbm.connect(db)
    client = EbbClient()
    try:
        failures = _pull_all(conn, client, day, include_browser, include_heavy)
    finally:
        client.close()
        conn.close()
    if failures:
        log.warning("%d dataset pulls failed", failures)
    raise typer.Exit(1 if failures else 0)


@app.command()
def setup(
    start: str = typer.Option(..., help="Backfill window start, YYYY-MM-DD"),
    end: str = typer.Option(None, help="Backfill window end, YYYY-MM-DD (default today)"),
    include_browser: bool = typer.Option(False, "--include-browser",
                                         help="Include the Playwright-based Ruby pull in the "
                                              "current-state step"),
    include_heavy: bool = typer.Option(False, "--include-heavy",
                                       help="Include multi-MB tariff-PDF rate-value pulls in the "
                                            "current-state step"),
    skip_current: bool = typer.Option(False, "--skip-current",
                                      help="Skip the initial pull-all of current state; only "
                                           "init the DB and backfill history"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Bootstrap a database from scratch: create it, pull current state, then
    backfill every pipeline's history-capable datasets over the start..end window.

    Replaces the manual init-db + pull-all + per-pipeline backfill sequence.
    Kern River (rolling window) and Ruby history are not rebuildable and are
    only captured as current state. Widen --start for deeper history where the
    archives allow (NWP to 1998, SoCal to 2000, NGTL CER to 2006)."""
    d0, d1 = _parse_day(start), _parse_day(end)
    if d1 < d0:
        raise typer.BadParameter("end before start")
    conn = dbm.connect(db)  # creates the schema if the file is new (== init-db)
    client = EbbClient()
    current_failures = attempted = backfill_failures = 0
    try:
        if not skip_current:
            log.info("=== current state: pull-all ===")
            current_failures = _pull_all(conn, client, d1, include_browser, include_heavy)
        log.info("=== history: backfill %s..%s ===", d0, d1)
        attempted, backfill_failures = _backfill_all(conn, client, d0, d1)
    finally:
        client.close()
        conn.close()
    failures = current_failures + backfill_failures
    log.info("setup done: %d backfill datasets, %d failures (%d current, %d backfill)",
             attempted, failures, current_failures, backfill_failures)
    typer.echo(f"database ready at {db}")
    raise typer.Exit(1 if failures else 0)


@app.command()
def backfill(
    pipeline: str = typer.Option(..., "--pipeline", "-p"),
    dataset: str = typer.Option(..., "--dataset", "-d"),
    start: str = typer.Option(..., help="YYYY-MM-DD"),
    end: str = typer.Option(..., help="YYYY-MM-DD"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Pull a historical date range (datasets with archive support)."""
    d0, d1 = _parse_day(start), _parse_day(end)
    if d1 < d0:
        raise typer.BadParameter("end before start")
    conn = dbm.connect(db)
    client = EbbClient()
    try:
        ok = _pull_one(conn, client, pipeline.lower(), dataset, d1, start=d0, end=d1)
    finally:
        client.close()
        conn.close()
    raise typer.Exit(0 if ok else 1)


@app.command()
def dashboard(
    out: Path = typer.Option(Path("data/dashboard.html"), help="Output HTML path"),
    open_browser: bool = typer.Option(False, "--open", help="Open in default browser"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Render a self-contained HTML dashboard from the database."""
    from . import dashboard as dash
    conn = dbm.connect(db)
    html_text = dash.generate(conn)
    conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    typer.echo(f"wrote {out}")
    if open_browser:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


@app.command()
def alerts(
    since: str = typer.Option(None, help="ISO timestamp; default = last alerts run"),
    drop_pct: float = typer.Option(10.0, help="Alert on capacity drops bigger than this at key points"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Report what changed since the last alerts run: new critical notices/OFOs,
    rate-doc changes, and operating-capacity drops at key points."""
    conn = dbm.connect(db)
    watermark = since or dbm.get_meta(conn, "alerts_last_run", "1970-01-01")
    n_alerts = 0

    rows = conn.execute(
        """SELECT pipeline, category, subject, posted_at FROM notices
           WHERE category IN ('critical', 'ofo') AND retrieved_at > ?
           ORDER BY posted_at DESC""", (watermark,)).fetchall()
    if rows:
        typer.echo(f"— {len(rows)} new critical/OFO notices —")
        for pipe, cat, subject, posted in rows[:30]:
            typer.echo(f"  [{pipe}] {cat.upper()} {posted}  {subject[:110]}")
        n_alerts += len(rows)

    rows = conn.execute(
        """SELECT pipeline, doc_type, title, changed_at FROM rate_docs
           WHERE changed_at IS NOT NULL AND changed_at > ?""", (watermark,)).fetchall()
    if rows:
        typer.echo(f"— {len(rows)} rate/tariff documents changed —")
        for pipe, doc_type, title, changed in rows:
            typer.echo(f"  [{pipe}] {doc_type}: {title[:100]} ({changed[:10]})")
        n_alerts += len(rows)

    # rate-value parses that failed or shrank sharply (tariff layout change?)
    rows = conn.execute("""
        WITH runs AS (
            SELECT pipeline, ok, n_records, ran_at,
                   ROW_NUMBER() OVER (PARTITION BY pipeline ORDER BY ran_at DESC) AS rn
            FROM pull_log WHERE dataset = 'rate_values'
        )
        SELECT a.pipeline, a.ok, a.n_records, b.n_records, a.ran_at
        FROM runs a LEFT JOIN runs b ON b.pipeline = a.pipeline AND b.rn = 2
        WHERE a.rn = 1 AND a.ran_at > ?
          AND (a.ok = 0 OR (b.n_records > 0 AND a.n_records < b.n_records * 0.75))
    """, (watermark,)).fetchall()
    if rows:
        typer.echo(f"— {len(rows)} rate-value parses failed or shrank —")
        for pipe, ok, n_now, n_prev, ran in rows:
            what = "FAILED (layout change?)" if not ok else \
                f"shrank {n_prev} -> {n_now} values"
            typer.echo(f"  [{pipe}] rate_values {what} ({ran[:10]})")
        n_alerts += len(rows)

    # operating-capacity drops at key points: latest gas day vs the prior one
    rows = conn.execute("""
        WITH days AS (
            SELECT pipeline, location_id, gas_day, operating_cap,
                   ROW_NUMBER() OVER (PARTITION BY pipeline, location_id
                                      ORDER BY gas_day DESC) AS rn
            FROM v_utilization
            WHERE is_key_point=1 AND operating_cap > 0 AND scheduled_qty > 0
        )
        SELECT a.pipeline, a.location_id, u.display_name, b.gas_day, a.gas_day,
               b.operating_cap, a.operating_cap
        FROM days a JOIN days b
          ON a.pipeline=b.pipeline AND a.location_id=b.location_id
         AND a.rn=1 AND b.rn=2
        JOIN v_utilization u
          ON u.pipeline=a.pipeline AND u.location_id=a.location_id AND u.gas_day=a.gas_day
        WHERE a.operating_cap < b.operating_cap * (1 - ? / 100.0)
    """, (drop_pct,)).fetchall()
    if rows:
        typer.echo(f"— capacity drops >{drop_pct:.0f}% at key points —")
        for pipe, _loc, name, d_prev, d_now, cap_prev, cap_now in rows:
            typer.echo(f"  [{pipe}] {name}: {cap_prev:,.0f} ({d_prev}) -> "
                       f"{cap_now:,.0f} ({d_now})")
        n_alerts += len(rows)

    if n_alerts == 0:
        typer.echo(f"no alerts since {watermark}")
    dbm.set_meta(conn, "alerts_last_run", dbm.now_utc())
    conn.close()


@app.command()
def export(
    table: str = typer.Option(..., "--table", "-t",
                              help="capacity | flows | notices | rate_docs | v_utilization | v_capacity_latest"),
    out: Path = typer.Option(None, help="Output CSV path (default <table>.csv)"),
    pipeline: str = typer.Option(None, "--pipeline", "-p", help="Filter to one pipeline"),
    start: str = typer.Option(None, help="Min gas_day/posted_at (YYYY-MM-DD)"),
    end: str = typer.Option(None, help="Max gas_day/posted_at (YYYY-MM-DD)"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Export a table or view to CSV for Excel/pandas work."""
    import csv as csvmod
    allowed = {"capacity", "flows", "notices", "rate_docs", "locations",
               "pull_log", "v_utilization", "v_capacity_latest",
               "tariff_rates", "v_current_tariff_rates"}
    if table not in allowed:
        raise typer.BadParameter(f"table must be one of {sorted(allowed)}")
    date_col = {"notices": "posted_at", "rate_docs": "last_seen",
                "tariff_rates": "effective_date",
                "v_current_tariff_rates": "effective_date"}.get(table, "gas_day")
    clauses, params = [], []
    if pipeline:
        clauses.append("pipeline = ?"); params.append(pipeline.lower())
    if start:
        clauses.append(f"{date_col} >= ?"); params.append(start)
    if end:
        clauses.append(f"{date_col} <= ?"); params.append(end + "~")  # inclusive w/ timestamps
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = dbm.connect(db)
    cur = conn.execute(f"SELECT * FROM {table}{where}", params)  # noqa: S608 — table allow-listed
    out = out or Path(f"{table}.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csvmod.writer(fh)
        writer.writerow([c[0] for c in cur.description])
        n = 0
        for row in cur:
            writer.writerow(row)
            n += 1
    conn.close()
    typer.echo(f"wrote {n} rows to {out}")


# Corridor membership for the Constraints page: (pipeline, location_id) ->
# (corridor, position in flow order). Drives the small-multiples layout.
PBI_CORRIDORS = {
    ("ngtl", "WGAT"): ("north", 1),
    ("gtn", "3498"): ("north", 2), ("gtn", "3500"): ("north", 3),
    ("gtn", "28218"): ("north", 4), ("gtn", "1820"): ("north", 5),
    ("cgt", "malin"): ("north", 6), ("cgt", "redwood_path"): ("north", 7),
    ("epng", "160"): ("south", 1), ("epng", "300"): ("south", 2),
    ("epng", "320"): ("south", 3), ("transwestern", "10487"): ("south", 4),
    ("socal", "needles_topock_area"): ("south", 5),
    ("socal", "transwestern_needles"): ("south", 5),
    ("socal", "el_paso_ehrenberg"): ("south", 5),
    ("cgt", "topock"): ("south", 6),
    ("ruby", "10"): ("rockies", 1), ("ruby", "60"): ("rockies", 2),
    ("kernriver", "054001"): ("rockies", 1), ("kernriver", "025011"): ("rockies", 3),
    ("kernriver", "025032"): ("rockies", 3), ("kernriver", "024011"): ("rockies", 3),
    ("socal", "kern_river_wheeler_ridge"): ("rockies", 4),
    ("socal", "kern_river_kramer_junction"): ("rockies", 4),
    ("cgt", "kern_river_station"): ("rockies", 5),
    ("cgt", "daggett"): ("rockies", 5), ("cgt", "onyx_hill"): ("rockies", 5),
    ("nwp", "28219"): ("pnw", 1), ("nwp", "28164"): ("pnw", 2),
}


@app.command("export-powerbi")
def export_powerbi(
    out_dir: Path = typer.Option(Path("data/powerbi"), help="Output folder for the CSV set"),
    days: int = typer.Option(730, help="History window for the capacity/flows facts"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Write a model-ready CSV set for Power BI (star-ish schema, stable columns).

    Chain after pulls (e.g. in the Task Scheduler runner) and point Power BI
    at the folder — see docs/powerbi.md for the model and suggested measures."""
    import csv as csvmod
    conn = dbm.connect(db)
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    def dump(name: str, cur, extra_cols=None, extra_fn=None) -> None:
        path = out_dir / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csvmod.writer(fh)
            cols = [c[0] for c in cur.description]
            w.writerow(cols + (extra_cols or []))
            n = 0
            for row in cur:
                w.writerow(list(row) + (extra_fn(dict(zip(cols, row))) if extra_fn else []))
                n += 1
        typer.echo(f"  {name}.csv: {n} rows")

    dump("capacity_daily", conn.execute("""
        SELECT pipeline || ':' || location_id AS location_key,
               pipeline, gas_day, cycle, location_id,
               COALESCE(NULLIF(location_name, ''), location_id) AS location_name,
               design_cap, operating_cap, scheduled_qty, available_cap, unit,
               CASE WHEN operating_cap > 0
                    THEN 1.0 * scheduled_qty / operating_cap END AS utilization,
               round(scheduled_qty / 1030.0, 1) AS scheduled_mmcfd,
               round(operating_cap / 1030.0, 1) AS capacity_mmcfd
        FROM v_capacity_latest WHERE gas_day >= ?""", (cutoff,)))
    dump("locations", conn.execute("""
        SELECT pipeline || ':' || location_id AS location_key, pipeline, location_id,
               location_type, display_name, is_key_point, interconnect_group
        FROM locations"""),
        extra_cols=["corridor", "position"],
        extra_fn=lambda r: list(PBI_CORRIDORS.get((r["pipeline"], r["location_id"]),
                                                  ("", ""))))
    dump("flows", conn.execute(
        """SELECT pipeline, gas_day, area, kind, flow, capability, unit
           FROM flows WHERE gas_day >= ?""", (cutoff,)))
    dump("notices", conn.execute("""
        SELECT pipeline, notice_id, category, subject, posted_at,
               effective_start, effective_end, url FROM notices"""))
    dump("tariff_rates_current", conn.execute("SELECT * FROM v_current_tariff_rates"))
    dump("tariff_rates_history", conn.execute("SELECT * FROM tariff_rates"))
    dump("pull_health", conn.execute("""
        SELECT pipeline, dataset, MAX(ran_at) AS last_run, ok, n_records
        FROM pull_log GROUP BY pipeline, dataset"""))
    # currently effective rate documents — same selection logic as `gaswatch rates`
    from . import rates as ratesmod
    docs = ratesmod.effective_rate_docs(conn)
    with open(out_dir / "rate_docs_current.csv", "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csvmod.writer(fh)
        cols = ["pipeline", "doc_type", "title", "url", "effective", "status",
                "changed_at", "first_seen"]
        w.writerow(cols)
        for d in docs:
            w.writerow([d[c] for c in cols])
    typer.echo(f"  rate_docs_current.csv: {len(docs)} rows")

    # briefing feed: one table, five sources, bucketed for the grouped view
    feed: list[tuple] = []
    for eff, subj, pipe in conn.execute("""
            SELECT COALESCE(NULLIF(effective_start,''), posted_at), subject, pipeline
            FROM notices WHERE category='ofo'
              AND COALESCE(NULLIF(effective_start,''), posted_at) >= date('now','-1 day')"""):
        feed.append(("1-pending", eff, "OFO", pipe, subj, ""))
    for posted, cat, subj, pipe, url in conn.execute("""
            SELECT posted_at, category, subject, pipeline, url FROM notices
            WHERE category IN ('critical','ofo')
              AND posted_at >= datetime('now','-36 hours')"""):
        feed.append(("2-overnight", posted, cat.upper(), pipe, subj, url or ""))
    for pipe, s, e, subj in conn.execute("""
            SELECT pipeline, substr(effective_start,1,10), substr(effective_end,1,10),
                   COALESCE(NULLIF(subject,''), substr(body_text,1,120)) FROM notices
            WHERE category IN ('maintenance','planned_outage')
              AND substr(effective_start,1,10) BETWEEN date('now') AND date('now','+2 day')"""):
        feed.append(("3-opening", s, "MAINT", pipe, f"{subj} -> {e}", ""))
    for pipe, title, changed in conn.execute("""
            SELECT pipeline, title, substr(changed_at,1,10) FROM rate_docs
            WHERE changed_at >= date('now','-3 day')"""):
        feed.append(("4-rate", changed, "RATE", pipe, title, ""))
    for pipe, name, util in conn.execute("""
            SELECT c.pipeline, COALESCE(l.display_name, c.location_id),
                   round(100.0 * c.scheduled_qty / c.operating_cap, 1)
            FROM v_capacity_latest c
            JOIN locations l ON l.pipeline=c.pipeline AND l.location_id=c.location_id
            WHERE l.is_key_point=1 AND c.operating_cap > 0
              AND c.gas_day >= date('now','-2 day')
              AND 1.0 * c.scheduled_qty / c.operating_cap >= 0.95"""):
        feed.append(("1-pending", dbm.now_utc()[:10], "CONSTRAINT", pipe,
                     f"{name} at {util}% of capacity", ""))
    with open(out_dir / "feed.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csvmod.writer(fh)
        w.writerow(["bucket", "when", "type", "pipeline", "what", "url"])
        for row in sorted(feed):
            w.writerow(row)
    typer.echo(f"  feed.csv: {len(feed)} rows")
    conn.close()
    typer.echo(f"Power BI CSV set written to {out_dir}")


@app.command()
def rates(
    pipeline: str = typer.Option(None, "--pipeline", "-p", help="Filter to one pipeline"),
    show_all: bool = typer.Option(False, "--all",
                                  help="Include superseded documents (older postings "
                                       "in the same series)"),
    urls: bool = typer.Option(False, "--urls", help="Print document URLs"),
    values: bool = typer.Option(False, "--values",
                                help="Show parsed rate values (reservation/usage/fuel) "
                                     "instead of documents; populate with "
                                     "'pull -d rate_values'"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Show the currently effective rate/tariff documents for each pipeline.

    Effective dates are parsed from document titles; within a series (e.g.
    monthly index rates, rate matrices) only the posting in effect today is
    shown. Undated standing documents (entire tariffs, rate schedules) are
    always listed. With --values, shows the actual rate values parsed from
    the rate sheets instead."""
    from . import rates as ratesmod
    conn = dbm.connect(db)
    if values:
        _print_rate_values(conn, pipeline)
        conn.close()
        return
    rows = ratesmod.effective_rate_docs(conn, pipeline=pipeline,
                                        include_superseded=show_all)
    conn.close()
    if not rows:
        typer.echo("no rate documents tracked yet — run pulls first")
        raise typer.Exit(1)
    current = None
    for r in rows:
        if r["pipeline"] != current:
            current = r["pipeline"]
            typer.echo(f"\n=== {current} ===")
        eff = r["effective"] or "-"
        flag = {"pending": "  [PENDING]", "superseded": "  [superseded]"}.get(r["status"], "")
        changed = f"  (changed {r['changed_at'][:10]})" if r["changed_at"] else ""
        typer.echo(f"  {r['doc_type']:14s} {eff:10s}  {r['title'][:90]}{flag}{changed}")
        if urls:
            typer.echo(f"                 {' ' * 10}  {r['url']}")


def _print_rate_values(conn, pipeline: str | None) -> None:
    where, params = "", []
    if pipeline:
        where = " WHERE pipeline = ?"
        params.append(pipeline.lower())
    rows = conn.execute(
        f"""SELECT pipeline, rate_schedule, component, path, qualifier, value, unit,
                   effective_date
            FROM v_current_tariff_rates{where}
            ORDER BY pipeline, rate_schedule, path, component, qualifier""",
        params).fetchall()  # noqa: S608 — where clause is parameterized
    if not rows:
        typer.echo("no parsed rate values yet — run: gaswatch pull -p <pipeline> -d rate_values")
        raise typer.Exit(1)
    current = None
    for pipe, sched, comp, path, qual, value, unit, eff in rows:
        if pipe != current:
            current = pipe
            typer.echo(f"\n=== {current} ===")
        val = f"{value:,.4f}".rstrip("0").rstrip(".") if value is not None else "-"
        typer.echo(f"  {sched:16s} {comp:12s} {qual:3s} {val:>12s} {unit:11s} "
                   f"{path[:60]}{'  eff=' + eff if eff else ''}")


@app.command()
def health(
    stale_hours: float = typer.Option(30.0, help="Flag datasets not pulled in this many hours"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Exit non-zero if any pipeline/dataset is stale or its last pull failed."""
    conn = dbm.connect(db)
    rows = conn.execute("""
        SELECT pipeline, dataset, MAX(ran_at), ok FROM pull_log
        GROUP BY pipeline, dataset""").fetchall()
    conn.close()
    problems = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    for pipeline, dataset, ran_at, ok in rows:
        try:
            last = datetime.strptime(ran_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last = None
        if not ok:
            problems.append(f"{pipeline}/{dataset}: last pull FAILED at {ran_at}")
        elif last and last < cutoff:
            problems.append(f"{pipeline}/{dataset}: stale (last ok {ran_at})")
    if not rows:
        problems.append("no pulls logged at all")
    for p in problems:
        typer.echo(p)
    if not problems:
        typer.echo(f"healthy: {len(rows)} pipeline/datasets all pulled within "
                   f"{stale_hours:.0f}h")
    raise typer.Exit(1 if problems else 0)


@app.command("clean-raw")
def clean_raw(
    keep_days: int = typer.Option(30, help="Delete raw archived responses older than this"),
    raw_dir: Path = typer.Option(Path("data/raw")),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Prune data/raw/ response archives past the retention window."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    n, freed = 0, 0
    for f in raw_dir.rglob("*"):
        if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            n += 1
            freed += f.stat().st_size
            if not dry_run:
                f.unlink()
    typer.echo(f"{'would delete' if dry_run else 'deleted'} {n} files "
               f"({freed / 1e6:.1f} MB) older than {keep_days} days")


@app.command()
def status(db: Path = typer.Option(dbm.DEFAULT_DB)):
    """Show last pull per pipeline/dataset and table row counts."""
    conn = dbm.connect(db)
    rows = conn.execute(
        """SELECT pipeline, dataset, MAX(ran_at), ok, n_records FROM pull_log
           GROUP BY pipeline, dataset ORDER BY pipeline, dataset"""
    ).fetchall()
    if not rows:
        typer.echo("no pulls logged yet")
    for pipeline, dataset, ran_at, ok, n in rows:
        typer.echo(f"{pipeline:10s} {dataset:12s} last={ran_at} ok={bool(ok)} records={n}")
    typer.echo("-" * 50)
    for table in ("capacity", "flows", "notices", "rate_docs", "tariff_rates"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        typer.echo(f"{table:10s} {count} rows")
    conn.close()


if __name__ == "__main__":
    app()
