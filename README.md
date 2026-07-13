# gaswatch — Western Gas Pipeline EBB Scraper

Pulls operational data from the public Electronic Bulletin Boards of ten western
natural gas pipeline systems into a local SQLite database:

| Pipeline | Operator / EBB | Method | Datasets |
|---|---|---|---|
| `cgt` | PG&E California Gas Transmission (Pipe Ranger) | HTTP (JSON servlets + HTML) | capacity, flows, ofo, maintenance, rates |
| `gtn` | TC Energy Gas Transmission Northwest (tcplus.com) | HTTP (JSON) | capacity¹, notices, rates |
| `ngtl` | TC Energy NOVA / NGTL (Daily Operating Plan API) | HTTP (JSON/CSV) | flows, capacity, outages, rates |
| `foothills` | TC Energy Foothills BC & SK (same DOP API) | HTTP (JSON/CSV) | flows, capacity, outages, notices, rates |
| `epng` | Kinder Morgan El Paso Natural Gas (DART) | HTTP (HTML grids) | capacity¹, points¹, notices, rates |
| `ruby` | Tallgrass Ruby Pipeline | **Playwright** (Imperva-protected) | capacity¹, notices, rates |
| `transwestern` | Energy Transfer Transwestern (Messenger/ipost) | HTTP (CSV exports) | capacity¹, notices, rates |
| `kernriver` | BHE Kern River (services portal) | HTTP (HTML + PDF) | capacity², notices, rates |
| `nwp` | Williams Northwest Pipeline (NWP_Portal) | HTTP (HTML + text) | capacity¹ (history to 1998), notices, rates |
| `socal` | SoCal Gas ENVOY (Sempra) | HTTP (CSV exports) | capacity, flows, maintenance, notices (incl. OFO), rates |

¹ NAESB-style postings where the Total Scheduled Quantity column is the flow data —
capacity and scheduled volumes come from the same posting.
² Kern River's OAC download form is reCAPTCHA-gated (an explicit anti-bot control we
respect); the adapter instead reads the public Daily Operational Report — per
constraint point, rolling 3-gas-day window, latest cycle. No public history.
Transwestern's capacity CSV serves historical gas days back to at least 2015
(`backfill -p transwestern -d capacity`).

Every pipeline additionally exposes a `rate_values` dataset (parsed tariff rate
values — see below), `ngtl` and `foothills` have `cer` (official CER key-point
history, 2006–present), and `socal` has `capacity_archive` (monthly workbooks
back to 2000). `gaswatch pipelines` prints the authoritative dataset list per
pipeline.

Everything except Ruby is plain HTTP — no browser, no login. Ruby's EBB sits behind
an Imperva Incapsula JS challenge, so its datasets run through headless Chromium and
are skipped by `pull-all` unless you pass `--include-browser`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# only if you want Ruby data:
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Usage

```powershell
.\.venv\Scripts\gaswatch.exe init-db                     # create data/gaswatch.db
.\.venv\Scripts\gaswatch.exe pipelines                   # list pipelines + datasets
.\.venv\Scripts\gaswatch.exe pull -p gtn -d capacity     # one dataset, today
.\.venv\Scripts\gaswatch.exe pull -p gtn -d capacity --gas-day 2026-07-01
.\.venv\Scripts\gaswatch.exe pull-all                    # everything except Ruby
.\.venv\Scripts\gaswatch.exe pull-all --include-browser  # everything incl. Ruby
.\.venv\Scripts\gaswatch.exe status                      # last pull + row counts
.\.venv\Scripts\gaswatch.exe dashboard --open            # render data/dashboard.html
.\.venv\Scripts\gaswatch.exe alerts                      # what changed since last alerts run
.\.venv\Scripts\gaswatch.exe rates                       # currently effective rate/tariff docs
.\.venv\Scripts\gaswatch.exe rates -p transwestern --all --urls
.\.venv\Scripts\gaswatch.exe export -t v_utilization -p gtn --start 2026-07-01
```

`alerts` diffs against its own last run (watermark in the `meta` table): new
critical/OFO notices, changed rate docs, and >10% operating-capacity drops at
key points (`--drop-pct` to tune). `export` writes any table or view to CSV.

`rates` shows the rate/tariff documents currently in effect on each pipeline.
The view is built from the tracked postings: only documents the latest pull
still saw on the EBB, with the effective date parsed from the title where one
is stated ("TW Rate Matrix Effective 4/1/2023", "Index Rates 2026-06", …).
Within a document series only the newest posting whose date has arrived is
shown — older ones are superseded (`--all` to include), future-dated ones are
flagged PENDING. Undated standing documents (entire tariffs, rate schedules,
GT&C) always count as current. The same view appears on the dashboard under
"Reference — currently effective rates".

### Parsed rate values (reservation / usage / fuel)

The `rate_values` dataset goes one level deeper: it downloads each pipeline's
statement of rates or rate matrix PDF and parses the actual numbers into the
`tariff_rates` table — reservation, usage/commodity, overrun, fuel %, storage
(inventory/injection/withdrawal), demand and surcharge components, with
max/min qualifiers, units, and the stated effective date. All ten systems are
covered: CGT (annual rate sheet), EPNG (CER statement-of-rates pages located
via the tariff PDF's named destinations), Kern River, GTN, NWP, Ruby
(statement-of-rates sheets; Ruby's goes through Playwright like its other
datasets), Transwestern (zone-to-zone maximum rate matrix incl. fuel), NGTL
and Foothills (headline tolls / per-zone table of effective rates; NGTL
per-point rates live in attachment PDFs), and SoCal (Schedule G-BTS backbone
rates via the CPUC tariff-book JSON API — fixed rates only; market-based caps
are skipped). The same API feeds the `socal` `rates` dataset, which
change-tracks the transmission/storage schedules (G-BTS, G-BSS, G-AUC, G-LTS,
G-TBS, G-SMT) by filed-sheet counts.

```powershell
.\.venv\Scripts\gaswatch.exe pull -p epng -d rate_values   # one pipeline
.\.venv\Scripts\gaswatch.exe pull-all --include-heavy      # all except Ruby
.\.venv\Scripts\gaswatch.exe pull-all --include-heavy --include-browser  # incl. Ruby
.\.venv\Scripts\gaswatch.exe rates --values -p cgt         # display current values
.\.venv\Scripts\gaswatch.exe export -t v_current_tariff_rates
```

`pull-all` skips `rate_values` by default (`HEAVY_DATASETS`) — tariff rates
change a handful of times a year, so schedule a weekly `--include-heavy` run.
History is kept per effective date; `v_current_tariff_rates` returns the
newest filing already in force.

**Hardening against layout changes.** Parsers only emit values from lines
that match the layout they were written against, and every parse then runs
through per-pipeline sanity checks (`ratesheets.EXPECTATIONS`): minimum
record count, required schedule/component pairs, value-range bounds, and a
parsed effective date. A check failure raises — the pull is logged FAILED and
the previously stored values stay in place, so a tariff refiling that changes
a PDF's layout can never silently store wrong or partial numbers. `alerts`
flags any rate_values pull that failed or shrank >25% vs the prior run, and
`health` already reports failed/stale pulls. Parser regressions are pinned by
fixture tests over saved page text (`tests/fixtures/*_rate*.txt`).

`export-powerbi` writes a model-ready CSV set (capacity/flows facts, a
locations dimension, notices, current + historical tariff rates, pull health)
to `data/powerbi/` for Power BI import — chain it after pulls and hit Refresh.
Model, relationships, and DAX measures that mirror the dashboard panels are in
[docs/powerbi.md](docs/powerbi.md).

`dashboard` renders a self-contained HTML page (light/dark aware, no external
assets): OFO status, notice/maintenance/rate-change counts, capacity-utilization
bars for the top points on each pipeline, current maintenance, recent critical
notices, and pull health. Regenerate it after each pull — e.g., chain
`gaswatch pull-all; gaswatch dashboard` in Task Scheduler.

Notice **body text** is captured automatically: GTN/NGTL/Foothills/CGT feeds
carry it inline; EPNG bodies come from per-notice detail pages and Ruby bodies
from the PDF notices (extracted with pypdf). Enrichment runs after each notices
pull and only fetches bodies that are still missing.

### Backfill (historical ranges)

```powershell
# PG&E final daily capacity/scheduled (archive reaches back to 2021-07-26)
.\.venv\Scripts\gaswatch.exe backfill -p cgt -d capacity --start 2024-01-01 --end 2026-07-01
# PG&E supply/demand (receipts by interconnect, demand, inventory)
.\.venv\Scripts\gaswatch.exe backfill -p cgt -d flows --start 2024-01-01 --end 2026-07-01
# GTN capacity for a date range (loops the daily JSON endpoint, all cycles)
.\.venv\Scripts\gaswatch.exe backfill -p gtn -d capacity --start 2026-06-01 --end 2026-07-01
# NGTL flows vs capability for a range
.\.venv\Scripts\gaswatch.exe backfill -p ngtl -d flows --start 2026-01-01 --end 2026-07-01
# EPNG: past flow days return the settled Cycle 7 view — history works too
.\.venv\Scripts\gaswatch.exe backfill -p epng -d capacity --start 2026-06-01 --end 2026-07-09
# CER official NGTL key-point history 2006-present (flows kind=cer_throughput)
.\.venv\Scripts\gaswatch.exe pull -p ngtl -d cer
# SoCal monthly archive workbooks (timely cycle) back to 2000
.\.venv\Scripts\gaswatch.exe backfill -p socal -d capacity_archive --start 2000-01-01 --end 2026-03-31
# NWP serves history to Jan 1998 (long crawl: ~1 req/s, one gas day each)
.\.venv\Scripts\gaswatch.exe backfill -p nwp -d capacity --start 1998-01-15 --end 2021-12-31
```

### Rebuilding the database from scratch (new machine)

The fastest path is copying `data/gaswatch.db` — it's one self-contained SQLite
file. To rebuild one year of history from the sources instead (adjust dates;
every archive below reaches at least a year back):

```powershell
.\.venv\Scripts\gaswatch.exe init-db
.\.venv\Scripts\gaswatch.exe pull-all --include-heavy --include-browser  # current state incl. rates
.\.venv\Scripts\gaswatch.exe backfill -p cgt -d capacity --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe backfill -p cgt -d flows    --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe backfill -p gtn -d capacity --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe backfill -p ngtl -d flows   --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe pull -p ngtl -d cer                       # official key points, one shot
.\.venv\Scripts\gaswatch.exe backfill -p transwestern -d capacity --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe backfill -p nwp -d capacity --start 2025-07-13 --end 2026-07-13
.\.venv\Scripts\gaswatch.exe backfill -p socal -d capacity_archive --start 2025-07-01 --end 2026-06-30
.\.venv\Scripts\gaswatch.exe backfill -p epng -d capacity --start 2025-07-13 --end 2026-07-13
```

Deeper history is available where the archives allow (NWP to 1998, SoCal
workbooks to 2000, NGTL CER to 2006, Transwestern to 2015, CGT to 2021-07-26,
GTN to ~2023) — widen the ranges above if you ever want it.

**Not rebuildable — copy the file if you care about these:** Kern River
capacity (rolling 3-gas-day window, no public archive — gaps are permanent),
Ruby history (browser pull, current posting only), notice bodies older than
each EBB's retention, and `tariff_rates` *history* (only the currently
effective filings are re-fetchable; superseded rate levels accrue over time).

Archive limits found empirically: CGT's XLSX archive 500s before ~2022 (nominally
back to 2021-07-26); GTN's JSON serves at least back to Jan 2023.

Known gaps (future work): EPNG intraday cycle selection and receipt-direction
points need the cycle-dropdown postback replicated; Ruby's TransFT/TransIT
contract postings would need a dedicated transactions table.

## Scheduling (Windows Task Scheduler — set up on the collection machine)

Deployment steps for whichever machine runs the scraper:

```powershell
# 1. clone + install (see Setup), then create a runner script, e.g. C:\gaswatch\run.cmd:
#      cd /d C:\gaswatch\gas-ops-scraper
#      .venv\Scripts\gaswatch.exe pull-all >> data\pull.log 2>&1
#      .venv\Scripts\gaswatch.exe alerts   >> data\alerts.log 2>&1
#      .venv\Scripts\gaswatch.exe dashboard

# 2. register the tasks (3x daily pulls + one nightly run incl. Ruby/Playwright):
schtasks /Create /TN "gaswatch-pull-am"  /SC DAILY /ST 07:00 /TR "C:\gaswatch\run.cmd"
schtasks /Create /TN "gaswatch-pull-pm"  /SC DAILY /ST 14:00 /TR "C:\gaswatch\run.cmd"
schtasks /Create /TN "gaswatch-pull-eve" /SC DAILY /ST 21:00 /TR "C:\gaswatch\run.cmd"
schtasks /Create /TN "gaswatch-browser"  /SC DAILY /ST 23:30 /TR `
  "C:\gaswatch\gas-ops-scraper\.venv\Scripts\gaswatch.exe pull-all --include-browser --db C:\gaswatch\gas-ops-scraper\data\gaswatch.db"

# 3. optional weekly retention pass:
schtasks /Create /TN "gaswatch-clean" /SC WEEKLY /D SUN /ST 06:00 /TR `
  "C:\gaswatch\gas-ops-scraper\.venv\Scripts\gaswatch.exe clean-raw --keep-days 30"

# 4. weekly rate-value refresh (multi-MB tariff PDF downloads; rates change rarely):
schtasks /Create /TN "gaswatch-rates" /SC WEEKLY /D SUN /ST 06:30 /TR `
  "C:\gaswatch\gas-ops-scraper\.venv\Scripts\gaswatch.exe pull-all --include-heavy --db C:\gaswatch\gas-ops-scraper\data\gaswatch.db"
```

Notes:
- In Task Scheduler's task properties, enable **"Run task as soon as possible
  after a scheduled start is missed"** so a sleeping machine catches up on wake.
  The machine must be on/awake for data to be collected — Kern River's daily
  report is a rolling 3-gas-day window with no history, so gaps there are
  permanent.
- Cadence rationale: capacity postings update per NAESB cycle (Timely
  ~afternoon, Evening ~night, intraday through the gas day); 7am/2pm/9pm
  catches the major cycles. `gaswatch health` exits non-zero if anything is
  stale or failing — useful as a monitoring hook.
- The client rate-limits itself to ~1 request/second/host — keep it that way.

## Data model (`data/gaswatch.db`)

- **capacity** — design/operating/scheduled/available per location per gas day per cycle.
  Unique on (pipeline, gas_day, cycle, location_type, location_id); re-pulls upsert.
- **flows** — actual/scheduled flow vs capability per area/path per gas day
  (NGTL/Foothills areas, CGT paths, CGT supply/demand metrics).
- **notices** — critical/non-critical/planned-outage/maintenance/OFO events, deduped
  by (pipeline, notice_id, category).
- **rate_docs** — tariff/rate documents with change detection: a row's `changed_at`
  updates (and the CLI logs `RATE DOC NEW/CHANGED`) when a document's hash/version moves.
- **tariff_rates** — rate values parsed from the rate sheets, keyed by
  (pipeline, rate_schedule, component, path, qualifier, effective_date); the
  `v_current_tariff_rates` view returns the newest filing already in force.
- **locations** — curated key-point dimension (display names, `is_key_point`,
  `interconnect_group` tying physically-connected points across pipelines);
  re-seeded idempotently from `db.KEY_LOCATIONS` on every connect.
- **raw_fetches / pull_log** — provenance and run history. Every parsed response is
  also archived under `data/raw/<pipeline>/<dataset>/` so parsers can be fixed and
  re-run without refetching.

Units are recorded per row (`Dth`, `MMBtu`, `MMcf`, `e3m3/d`, `e6m3/d` — Canadian
systems post metric).

## Fragility notes (undocumented endpoints)

- **NGTL/Foothills**: the DOP API base (`…execute-api.us-west-2.amazonaws.com/production`)
  is embedded in the SPA JS bundle and can rotate; the adapter rediscovers it
  automatically on failure.
- **EPNG**: grids page at 75 rows; further pages are fetched by replaying the
  Infragistics postback (`PageChange` clientState event). The clientState templates in
  [epng.py](gaswatch/pipelines/epng.py) were captured from live sessions; if Kinder Morgan
  reconfigures the grids, recapture them (see comments in the file).
- **CGT**: PG&E POST servlets need `X-Requested-With: XMLHttpRequest` + `Referer`
  headers (Akamai). Maintenance is parsed from the foghorn page HTML.
- **Ruby**: everything depends on Imperva tolerance of headless Chromium; the "Best
  Available" cycle renders an empty grid, so the adapter pulls Timely and Evening.
- All parsers fail loudly and archive raw responses; check `pull_log` for errors.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

Parser tests run against saved fixture responses in `tests/fixtures/` — no network.
