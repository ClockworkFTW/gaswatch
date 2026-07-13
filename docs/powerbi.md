# gaswatch → Power BI

The HTML dashboard's panels are all queries over `data/gaswatch.db`; this guide
rebuilds them in Power BI on top of the CSV set written by
`gaswatch export-powerbi` (default `data/powerbi/`).

## Refresh pipeline

Chain the export after pulls in the Task Scheduler runner (`run.cmd`):

```bat
.venv\Scripts\gaswatch.exe pull-all            >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe export-powerbi      >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe dashboard
```

Then in Power BI: **Refresh** re-reads the CSVs. For unattended refresh of a
published report, install the **on-premises data gateway (personal mode)** on
the collection machine and schedule dataset refresh in the Power BI service —
the CSVs are just files, so no driver or connection string is involved.

## Files and model

| File | Grain | Role |
|---|---|---|
| `capacity_daily.csv` | pipeline × gas_day × location (latest cycle) | fact — corridor minis (incl. `scheduled_mmcfd` / `capacity_mmcfd`, Dth ÷ 1.03) |
| `flows.csv` | pipeline × gas_day × area × kind | fact — NGTL flows, CGT/SoCal supply, storage |
| `feed.csv` | event, bucketed | the Briefing page (pending / overnight / opening / rate / constraint) |
| `notices.csv` | notice | maintenance timeline + tables |
| `tariff_rates_current.csv` | rate value in force | fact — rates matrix |
| `tariff_rates_history.csv` | rate value × effective_date | fact — rate history |
| `rate_docs_current.csv` | effective rate document | reference table with URLs |
| `locations.csv` | location (incl. `corridor` + `position`) | dimension — drives corridor small multiples |
| `pull_health.csv` | pipeline × dataset | ops health table |

Model setup (Get Data → Text/CSV for each file, or Folder → combine):

1. Relate `capacity_daily[location_key]` → `locations[location_key]`
   (many-to-one, single direction).
2. Create a date table: **Modeling → New table** →
   `Dates = CALENDAR(DATE(2020,1,1), TODAY())`, mark it as the date table, and
   relate `Dates[Date]` → `capacity_daily[gas_day]` and `flows[gas_day]`
   (set both CSV date columns to type Date first).
3. Nothing else needs relationships — notices/rates slice by their own
   `pipeline` column, or add a tiny `Pipelines` table if you want one slicer
   across everything: `Pipelines = DISTINCT(UNION(VALUES(capacity_daily[pipeline]), VALUES(notices[pipeline])))`.

## Measures (DAX)

```dax
Scheduled Dth   = SUM ( capacity_daily[scheduled_qty] )
Operating Cap   = SUM ( capacity_daily[operating_cap] )
Utilization %   = DIVIDE ( [Scheduled Dth], [Operating Cap] )

-- utilization of key points only (mirrors the HTML dashboard's bars)
Key Point Util % =
CALCULATE ( [Utilization %], locations[is_key_point] = 1 )

-- OFO days per month (CGT panel): count of OFO notices by effective month
OFO Days =
CALCULATE (
    DISTINCTCOUNT ( notices[effective_start] ),
    notices[category] = "ofo"
)

-- rate changes in the last 30 days (briefing tile)
Recent Rate Changes =
COUNTROWS (
    FILTER ( rate_docs_current,
             rate_docs_current[changed_at] <> ""
               && DATEVALUE ( LEFT ( rate_docs_current[changed_at], 10 ) )
                  >= TODAY () - 30 )
)

-- stale pulls (health tile; pull_health[last_run] is ISO UTC text)
Stale Pulls =
COUNTROWS (
    FILTER ( pull_health,
             DATEVALUE ( LEFT ( pull_health[last_run], 10 ) ) < TODAY () - 2
               || pull_health[ok] = 0 )
)
```

## The five pages

1. **Briefing** — one table over `feed.csv`, grouped by `bucket`
   (pending / posted overnight / windows opening / rate filings / constraint
   flags). Type chips via conditional icons; service data alert on pending
   OFO count > 0.
2. **Supply Paths** — flows only. Per corridor (`locations[corridor]`), a
   line chart with **small multiples by point** (sorted on `position`, always
   basin → California), two measures in absolute units:
   `SUM(capacity_daily[scheduled_mmcfd])` shaded and
   `SUM(capacity_daily[capacity_mmcfd])` as the line, shared y-axis. NGTL's
   pane uses `flows` (`flow × 0.0353`, `capability × 0.0353`). Ranked
   scheduled-vs-capacity stacked bar below (scheduled + headroom = capacity),
   sorted by utilization, serious color ≥ 90%. (In-state production and
   storage series — `california_production`, `elk_hills_wheeler_ridge`,
   `ending_inventory`, `ending_storage_balance_mcf` — stay in `flows.csv`
   for a future Storage page if wanted.)
3. **Maintenance** — Microsoft's free AppSource **Gantt** visual (task =
   subject, start/end = effective dates, legend = pipeline) over `notices`
   filtered to maintenance/planned_outage; native timeline-matrix fallback;
   items table below.
4. **Rates** — matrix over `tariff_rates_current`: rows pipeline →
   rate_schedule → path, columns component, values max/min; slicer on
   qualifier; `rate_docs_current` table with `url` as Web URL category.
5. **Ops health** — table over `pull_health` with conditional formatting on
   `ok`/`last_run`; data alert on failed > 0.

Unit note: `*_mmcfd` columns are heat-content approximations (Dth ÷ 1.03,
NGTL 10³m³ × 0.0353) — right for corridor reasoning, not invoice math.

## Alternative: query SQLite directly

If you prefer no export step, install a SQLite ODBC driver
(e.g. <http://www.ch-werner.de/sqliteodbc/>), create a DSN pointing at
`data/gaswatch.db`, and use Get Data → ODBC. Import mode only; the views
`v_capacity_latest`, `v_utilization`, and `v_current_tariff_rates` are the
model-ready entry points. The CSV route is recommended because it needs no
driver on report-consumer machines and keeps refresh decoupled from the
scraper's write locks.
