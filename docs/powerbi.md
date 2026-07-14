# gaswatch → Power BI

The HTML dashboard's panels are all queries over `data/gaswatch.db`; this guide
rebuilds them in Power BI on top of the CSV set written by
`gaswatch export-powerbi` (default `data/powerbi/`). It's written to be followed
top-to-bottom by someone new to Power BI — every visual has click-by-click
steps. Sections 1–4 build the data model; section 5 onward builds the report.

## Refresh pipeline

Chain the export after pulls in the Task Scheduler runner (`run.cmd`):

```bat
.venv\Scripts\gaswatch.exe pull-all            >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe export-powerbi      >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe dashboard
```

Then in Power BI: **Home → Refresh** re-reads the CSVs. For unattended refresh of
a published report, install the **on-premises data gateway (personal mode)** on
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

---

## 1. Load the CSVs

Load each file as its **own table** — do **not** use Get Data → Folder →
Combine. "Combine" stacks files that share one schema; these nine have
different columns, so combining them produces garbage.

For each of the nine files: **Home → Get Data → Text/CSV**, browse to
`data\powerbi\`, pick the file, click **Load** (not "Transform Data" yet):

`capacity_daily.csv`, `flows.csv`, `locations.csv`, `notices.csv`,
`tariff_rates_current.csv`, `tariff_rates_history.csv`, `rate_docs_current.csv`,
`pull_health.csv`, `feed.csv`.

When done, the **Data** pane (right) lists nine tables.

## 2. Fix data types

Blank cells in the numeric columns make the Data-view "change type" fail
wholesale, so do type conversions in the **Power Query Editor**, which converts
cell-by-cell and lets blanks become `null`.

1. **Home → Transform data** (opens Power Query Editor).
2. Select the **`capacity_daily`** query (left). Ctrl-click to multi-select these
   headers: `design_cap`, `operating_cap`, `scheduled_qty`, `available_cap`,
   `scheduled_mmcfd`, `capacity_mmcfd`, `utilization`.
3. **Transform → Data Type → Decimal Number** (Replace current if prompted). Blank
   cells become `Error`.
4. With those columns still selected: **Transform → Replace Values ▾ → Replace
   Errors → `null` → OK**. (`null` is ignored by `SUM`, so a missing design cap
   correctly contributes 0 — not a literal 0 that would drag averages down.)
5. Set **`capacity_daily[gas_day]`** and **`flows[gas_day]`** to **Date** (click the
   type icon left of the header → Date). These are clean and won't error.
6. On **`flows`**, set `flow` and `capability` to Decimal Number + Replace Errors → null.
7. Leave **`notices`** date columns (`posted_at`, `effective_start`, `effective_end`)
   as **Text** — `effective_end` holds values like `TBD` and some are blank, so a
   Date conversion would error, and the tables/timeline don't need them typed.
   (The Maintenance Gantt in section 7.3 adds its own date columns.)
8. **Home → Close & Apply**.

## 3. Model — relationships and a date table

Switch to **Model view** (third icon, left edge).

1. Drag **`capacity_daily[location_key]`** onto **`locations[location_key]`**.
   Double-click the line to confirm: **Many-to-one (\*:1)**, **Cross-filter =
   Single**, arrow pointing at `capacity_daily`.
2. Create a date table: **Modeling → New table**, enter
   `Dates = CALENDAR(DATE(2020,1,1), TODAY())`. Select the `Dates` table →
   **Table tools → Mark as date table** → column `Date`.
3. Drag **`Dates[Date]`** onto **`capacity_daily[gas_day]`**, then **`Dates[Date]`**
   onto **`flows[gas_day]`**. Both are **One-to-many (1:\*)** with `Dates` on the
   "1" side, **Cross-filter = Single** (filter flows Dates → facts).
4. Nothing else needs relationships — `notices`, the rate tables, `pull_health`,
   and `feed` each slice by their own `pipeline` column. Optional: one slicer
   across everything via a tiny table —
   `Pipelines = DISTINCT(UNION(VALUES(capacity_daily[pipeline]), VALUES(notices[pipeline])))`.

## 4. Measures (DAX)

In the **Data** pane, right-click **`capacity_daily` → New measure** for each of
these (paste, Enter). Keeping them all on `capacity_daily` is fine.

```dax
Scheduled Dth   = SUM ( capacity_daily[scheduled_qty] )
Operating Cap   = SUM ( capacity_daily[operating_cap] )
Utilization %   = DIVIDE ( [Scheduled Dth], [Operating Cap] )

-- utilization of key points only (mirrors the HTML dashboard's bars)
Key Point Util % =
CALCULATE ( [Utilization %], locations[is_key_point] = 1 )

-- unused headroom, for the ranked scheduled-vs-capacity stacked bar
Headroom Dth = [Operating Cap] - [Scheduled Dth]

-- OFO days per month (CGT panel): count of OFO notices by effective month
OFO Days =
CALCULATE ( DISTINCTCOUNT ( notices[effective_start] ), notices[category] = "ofo" )

-- rate changes in the last 30 days (briefing tile)
Recent Rate Changes =
COUNTROWS (
    FILTER ( rate_docs_current,
             rate_docs_current[changed_at] <> ""
               && DATEVALUE ( LEFT ( rate_docs_current[changed_at], 10 ) )
                  >= TODAY () - 30 ) )

-- stale pulls (health tile; pull_health[last_run] is ISO UTC text)
Stale Pulls =
COUNTROWS (
    FILTER ( pull_health,
             DATEVALUE ( LEFT ( pull_health[last_run], 10 ) ) < TODAY () - 2
               || pull_health[ok] = 0 ) )
```

Format `Utilization %` and `Key Point Util %` as Percentage (**Measure tools →
Format → Percentage**); format the Dth measures as Whole number with a thousands
separator.

---

## 5. Building the report — the workflow

Every visual is built the same way, in **Report view** (top icon):

> **Click an empty spot on the canvas → pick a visual type in the
> Visualizations pane → drag fields from the Data pane into the visual's wells
> (Axis / Values / Legend / etc.).** Filter with the **Filters** pane; sort with
> the visual's **⋯ → Sort axis**.

Rename a page by double-clicking its tab at the bottom; add a page with the
**+**. A **Slicer** visual bound to `pipeline` (or `Dates[Date]`) filters every
visual on its page.

Set any URL column to render as a link: Data view → select the column
(`feed[url]`, `rate_docs_current[url]`) → **Column tools → Data category → Web
URL**.

---

## 6. Page 1 — Briefing

One table over `feed`, grouped by bucket (pending → posted overnight → windows
opening → rate filings → constraint flags).

1. New page, name it **Briefing**.
2. Canvas → **Visualizations → Table**.
3. Tick, in order: `feed[bucket]`, `feed[when]`, `feed[type]`, `feed[pipeline]`,
   `feed[what]`, `feed[url]`.
4. Sort by bucket: visual **⋯ → Sort axis → bucket → Ascending** (the bucket
   prefixes `1-…`,`2-…` order the groups correctly).
5. Optional emphasis: **Format your visual → Cell elements**, or right-click the
   `type` field → **Conditional formatting → Background color** to chip OFO /
   CRITICAL rows.
6. Optional tile: canvas → **Card** → drag `feed` into **Fields**, set it to
   **Count**, then Filters pane → `bucket` is `1-pending` — a pending-items
   counter. (In the Power BI service you can set a data alert on it.)

## 7. Page 2 — Supply Paths

Two visuals: corridor small-multiples (flow vs capacity per point) and a ranked
scheduled-vs-capacity bar. Add a **Slicer** on `locations[corridor]` first so
both react to the selected corridor (`north`, `south`, `rockies`, `pnw`).

### 7.1 Corridor small multiples (capacity_daily)
1. Canvas → **Visualizations → Line chart**.
2. **X-axis:** `capacity_daily[gas_day]`.
3. **Y-axis:** `capacity_daily[scheduled_mmcfd]` and `capacity_daily[capacity_mmcfd]`
   (both auto-sum; scheduled = the shaded/lower series, capacity = the ceiling).
4. **Small multiples:** `locations[display_name]` — one mini-chart per point.
5. Sort the minis basin→California: in Data view select `locations[display_name]`
   → **Column tools → Sort by column → position**. Now the small multiples order
   by `position`.
6. The corridor slicer scopes it; or set a visual-level filter
   `locations[corridor] is south` and duplicate the visual per corridor for a
   four-panel layout.

> NGTL's flow lives in `flows`, not `capacity_daily`. For an NGTL pane, make a
> second line chart over `flows` (X = `gas_day`, Y = `flow` and `capability`,
> Small multiples = `area`), filtered to `flows[pipeline] is ngtl`. Values are
> 10³m³; multiply by 0.0353 in a measure if you want MMcf/d.

### 7.2 Ranked scheduled-vs-capacity bar (key points)
1. Canvas → **Visualizations → Stacked bar chart**.
2. **Y-axis:** `locations[display_name]`.
3. **X-axis (values):** measure `[Scheduled Dth]` then `[Headroom Dth]` — they
   stack to operating capacity (scheduled + headroom = capacity).
4. Filters pane → `locations[is_key_point]` **is 1**.
5. Sort by load: **⋯ → Sort axis → Scheduled Dth → Descending** (or add
   `[Utilization %]` to the tooltip and sort by it).
6. Flag constrained points: select the visual → **Format → Bars → Colors → fx
   (conditional)** on the `Scheduled Dth` series → **Format style: Rules** on
   `[Utilization %]`, e.g. `>= 0.9` → red, else the default color.

*(The simple "Utilization % by `display_name`" bar you already built is a fine
lightweight substitute for 7.2 — keep whichever you prefer.)*

Unit note: `*_mmcfd` columns are heat-content approximations (Dth ÷ 1.03, NGTL
10³m³ × 0.0353) — right for corridor reasoning, not invoice math.

## 8. Page 3 — Maintenance

A Gantt timeline of maintenance / planned outages. The Gantt needs **date-typed**
start/end, so add those columns first (we kept the text ones for the tables).

1. **Home → Transform data** → select `notices`. **Add Column → Custom Column**:
   - Name `start_date`, formula: `try Date.From([effective_start]) otherwise null`
   - Repeat: name `end_date`, formula: `try Date.From([effective_end]) otherwise null`
   Set both new columns' type to **Date**. **Close & Apply**.
2. Get the visual: **Insert → More visuals → Get more visuals**, search **Gantt**,
   add "Gantt Chart" (by Microsoft / MAQ — either free one works).
3. New page **Maintenance**. Drop the Gantt on the canvas and fill its wells:
   **Task** = `notices[subject]`, **Start Date** = `start_date`, **End Date** =
   `end_date`, **Legend** = `notices[pipeline]`.
4. Filters pane → `notices[category]` **is in** `maintenance`, `planned_outage`.
5. Below it, add a **Table**: `pipeline`, `subject`, `effective_start`,
   `effective_end`, `url` — the item list, same category filter.

> No AppSource access on the work machine? Use a native **Matrix** as a fallback:
> Rows = `pipeline` then `subject`, Columns = `start_date` (set the column's date
> hierarchy to Month), Values = a `Count` — a coarse month grid of what's out.

## 9. Page 4 — Rates

A matrix of current tariff values plus a linked document table.

1. New page **Rates**. Canvas → **Visualizations → Matrix**.
2. **Rows:** `tariff_rates_current[pipeline]`, then `[rate_schedule]`, then `[path]`
   (drag in that order to nest them).
3. **Columns:** `tariff_rates_current[component]`.
4. **Values:** `tariff_rates_current[value]`. Set its aggregation to **Maximum**
   (click the ▾ on the field → Maximum) so a cell shows the rate, not a sum.
5. Canvas → **Slicer** → `tariff_rates_current[qualifier]` (toggles max vs min
   rate levels).
6. Beside the matrix, a **Table** over `rate_docs_current`: `pipeline`, `doc_type`,
   `title`, `effective`, `status`, `url` (with `url` set to Web URL so titles
   link out).

## 10. Page 5 — Ops health

The pull-status table (you've already built this).

1. New page **Ops health**. Canvas → **Visualizations → Table**.
2. Fields: `pull_health[pipeline]`, `[dataset]`, `[last_run]`, `[ok]`, `[n_records]`.
3. Right-click `ok` in the Columns well → **Conditional formatting → Background
   color** → `1` green, `0` red. Optionally add a **Card** with the `[Stale Pulls]`
   measure as a health KPI (data-alert on it in the service).

---

## Publishing & refresh

**Home → Refresh** re-reads the CSVs after each `export-powerbi`. To keep a
*published* report current without opening the desktop, install the
**on-premises data gateway (personal mode)** on the collection machine and set a
scheduled refresh in the Power BI service — the CSVs are plain files, so no
driver or connection string is needed.

## Alternative: query SQLite directly

If you prefer no export step, install a SQLite ODBC driver
(e.g. <http://www.ch-werner.de/sqliteodbc/>), create a DSN pointing at
`data/gaswatch.db`, and use Get Data → ODBC. Import mode only; the views
`v_capacity_latest`, `v_utilization`, and `v_current_tariff_rates` are the
model-ready entry points. The CSV route is recommended because it needs no
driver on report-consumer machines and keeps refresh decoupled from the
scraper's write locks.
