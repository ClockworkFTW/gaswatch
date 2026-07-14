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
| `throughput.csv` | pipeline × gas_day × location × flow_direction × kind | **the** fact — every pipeline's flow/capacity, already in MMcf/d |
| `locations.csv` | location (incl. `corridor`, `position`, `basin`) | dimension — drives corridor small multiples |
| `system_metrics.csv` | pipeline × gas_day × area × kind | non-throughput series (storage, demand, inventory, imbalance, fuel, temperature — incl. SoCal's, which post under throughput kinds) |
| `feed.csv` | event, bucketed | the Briefing page (pending / overnight / opening / rate / constraint) |
| `notices.csv` | notice | maintenance timeline + tables |
| `tariff_rates_current.csv` | rate value in force | fact — rates matrix |
| `tariff_rates_history.csv` | rate value × effective_date | fact — rate history |
| `rate_docs_current.csv` | effective rate document | reference table with URLs |
| `pull_health.csv` | pipeline × dataset | ops health table |

### `throughput` — one normalized table, one unit

`throughput.csv` dumps the `v_throughput` view, which unions the two shapes the
EBBs actually publish and converts both to **MMcf/d**, so NGTL sits next to Kern
River in the same column:

| source | what | conversion |
|---|---|---|
| `capacity` postings (cgt, epng, gtn, kernriver, nwp, ruby, socal, transwestern) | scheduled vs operating capacity per point | Dth ÷ 1030 |
| `flows` — ngtl/foothills `actual` | flow vs capability per area | 10³m³/d × 0.0353147 |
| `flows` — socal point receipts (best of `actual` > `estimate` > `forecast`) | flow per border/producer point | Dth ÷ 1030 |
| `flows` — cgt `receipt` | volumes arriving at the CA border per interconnect | MMcf × 1 |
| `flows` — ngtl `cer_throughput` | official CER key-point history back to 2006 | 10³m³/d × 0.0353147 |

Conversion is driven by the source `unit` column (kept as `unit_src`); the
factor map lives in `db.UNIT_TO_MMCFD`, and `export-powerbi` **fails** if a
unit outside that map ever appears rather than exporting NULLs. `utilization`
is computed from the raw values, so it's unit-invariant.

Two curation rules keep the fact table honest:
- SoCal's daily-operations report mixes storage, demand, imbalance, fuel and
  temperature rows into the same `actual`/`estimate`/`forecast` kinds as its
  point receipts; those areas (plus NGTL's `LINEPACK`) are routed to
  `system_metrics.csv` instead (`db.NON_THROUGHPUT_AREAS`).
- SoCal posts the same gas day up to three times (forecast → estimate →
  actual); the flows side keeps **one row per area-day**, best kind wins.
  The raw `flows` table still holds all three if you ever need them.

Things to know about the grain:
- It is unique on **pipeline × gas_day × location_type × location_id ×
  location_name × flow_direction × kind**. Don't sum without a sensible filter.
- **`flow_direction` matters**: a point posts Receipt *and* Delivery (and NWP also
  posts Mainline/Bi-directional) — summing across them double-counts a point.
  Rows from the `flows` source carry a **blank** direction.
- **`kind`** separates the series (`timely`/`evening`/`id1…` cycles, `actual`,
  `cer_throughput`, `receipt`). NGTL's `actual`, `cer_throughput`, and `snapshot`
  use disjoint area names, so they never overlap — but they're different series,
  so slice by `kind` rather than blending them.
- **One point can appear from both sources**: `socal:el_paso_ehrenberg` has a
  `capacity` row (scheduled vs operating, from the scheduled-quantities report)
  *and* a `flows` row (actual physical flow, from daily operations) per day.
  They're different measures of the same point — filter on `source` (or `kind`)
  before summing anything that includes SoCal points.
- **NWP reuses short `location_id`s** (`1`, `2`, …) across differently-named
  segments; `location_name` is what distinguishes them.

---

## 1. Load the CSVs

Load each file as its **own table** — do **not** use Get Data → Folder →
Combine. "Combine" stacks files that share one schema; these nine have
different columns, so combining them produces garbage.

For each of the nine files: **Home → Get Data → Text/CSV**, browse to
`data\powerbi\`, pick the file, click **Load** (not "Transform Data" yet):

`throughput.csv`, `locations.csv`, `system_metrics.csv`, `notices.csv`,
`tariff_rates_current.csv`, `tariff_rates_history.csv`, `rate_docs_current.csv`,
`pull_health.csv`, `feed.csv`.

When done, the **Data** pane (right) lists nine tables.

## 2. Fix data types

Blank cells in the numeric columns make the Data-view "change type" fail
wholesale, so do type conversions in the **Power Query Editor**, which converts
cell-by-cell and lets blanks become `null`.

1. **Home → Transform data** (opens Power Query Editor).
2. Select the **`throughput`** query (left). Ctrl-click to multi-select these
   headers: `scheduled_mmcfd`, `capacity_mmcfd`, `design_mmcfd`,
   `available_mmcfd`, `utilization`.
3. **Transform → Data Type → Decimal Number** (Replace current if prompted). Blank
   cells become `Error`.
4. With those columns still selected: **Transform → Replace Values ▾ → Replace
   Errors → `null` → OK**. (`null` is ignored by `SUM`, so a point that posts no
   design capacity correctly contributes nothing — not a literal 0 that would
   drag averages down.)
5. Set **`throughput[gas_day]`** and **`system_metrics[gas_day]`** to **Date**
   (click the type icon left of the header → Date). These are clean and won't error.
6. On **`locations`**, set `position` to **Whole number** (needed to sort corridor
   points basin→border in §7).
7. Leave **`notices`** date columns (`posted_at`, `effective_start`, `effective_end`)
   as **Text** — `effective_end` holds values like `TBD` and some are blank, so a
   Date conversion would error, and the tables/timeline don't need them typed.
   (The Maintenance Gantt in section 7.3 adds its own date columns.)
8. **Home → Close & Apply**.

## 3. Model — relationships and a date table

Switch to **Model view** (third icon, left edge).

1. Drag **`throughput[location_key]`** onto **`locations[location_key]`**.
   Double-click the line to confirm: **Many-to-one (\*:1)**, **Cross-filter =
   Single**, arrow pointing at `throughput`. (`locations` holds only the ~44
   curated key points, so most bulk rows simply won't match — that's expected;
   the corridor pages filter to key points anyway.)
2. Create a date table: **Modeling → New table**, enter
   `Dates = CALENDAR(DATE(2020,1,1), TODAY())`. Select the `Dates` table →
   **Table tools → Mark as date table** → column `Date`.
3. Drag **`Dates[Date]`** onto **`throughput[gas_day]`**, then **`Dates[Date]`**
   onto **`system_metrics[gas_day]`**. Both are **One-to-many (1:\*)** with `Dates`
   on the "1" side, **Cross-filter = Single** (filter flows Dates → facts).
4. Nothing else needs relationships — `notices`, the rate tables, `pull_health`,
   and `feed` each slice by their own `pipeline` column. Optional: one slicer
   across everything via a tiny table —
   `Pipelines = DISTINCT(UNION(VALUES(throughput[pipeline]), VALUES(notices[pipeline])))`.

## 4. Measures (DAX)

In the **Data** pane, right-click **`throughput` → New measure** for each of
these (paste, Enter). Keeping them all on `throughput` is fine.

```dax
-- everything is already MMcf/d, so these are directly comparable across systems
Scheduled MMcfd = SUM ( throughput[scheduled_mmcfd] )
Capacity MMcfd  = SUM ( throughput[capacity_mmcfd] )
Utilization %   = DIVIDE ( [Scheduled MMcfd], [Capacity MMcfd] )

-- utilization of key points only (mirrors the HTML dashboard's bars)
Key Point Util % =
CALCULATE ( [Utilization %], locations[is_key_point] = 1 )

-- unused headroom, for the ranked scheduled-vs-capacity stacked bar
Headroom MMcfd = [Capacity MMcfd] - [Scheduled MMcfd]

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

Corridors trace the physical path from a **supply basin to the California border
interconnect** it serves, following the western pipeline map's Key Western
Interconnects (Sumas, Kingsgate, Opal, Malin, Topock, Needles, Ehrenberg):

| corridor | basin | path |
|---|---|---|
| `wcsb_malin` | WCSB | NGTL West Gate → AB/BC border → GTN Kingsgate → Stn 8 → **Malin** → CGT receipt → Redwood |
| `rockies_malin` | Rocky Mountain | Ruby: Opal → Tule Lake → **Malin** (CGT Onyx Hill) |
| `rockies_socal` | Rocky Mountain | Kern River: Muddy Creek/Opal → Daggett → **Wheeler Ridge / Kramer Jct** |
| `sanjuan_topock` | San Juan | EPNG: San Juan Triangle → Hackberry → **Topock** → CGT/SoCal |
| `permian_needles` | Permian | Transwestern mainline → **Needles** → SoCal |
| `permian_topock` | Permian | Transwestern north leg → **Topock** → CGT (same town as EPNG's Topock, different upstream system) |
| `permian_ehrenberg` | Permian | EPNG *south* mainline (Permian/Waha gas) → **Ehrenberg** → SoCal |
| `wcsb_pnw` | WCSB | NWP: **Sumas** → Mt. Vernon → Jackson Prairie (PNW, not CA) |

(Transwestern is dual-sourced — Permian mainline plus a San Juan lateral —
"Permian" is the working simplification. Red Hawk Plant stays a key location
but is off the corridor: it's a power-plant delivery near Palo Verde, AZ, not
a supply anchor.)

Because everything is one table in one unit, a single chart now covers **all**
systems — NGTL and Kern River appear in the same corridor, on the same axis.

Add a **Slicer** on `locations[corridor]` first (single-select) so both visuals
follow the chosen corridor. A `locations[basin]` slicer works too if you'd rather
compare by basin.

### 7.1 Corridor small multiples
1. Canvas → **Visualizations → Line chart**.
2. **X-axis:** `throughput[gas_day]`.
3. **Y-axis:** `throughput[scheduled_mmcfd]` and `throughput[capacity_mmcfd]`
   (scheduled = the flowing volume, capacity = the ceiling).
4. **Small multiples:** `locations[display_name]` — one mini-chart per point.
5. Sort the minis basin→border: Data view → `locations[display_name]` →
   **Column tools → Sort by column → `position`** (set `position` to Whole number
   first, per §2).
6. **Filters on this visual:** `locations[corridor]` — pick one (the slicer then
   swaps it). Also add `throughput[flow_direction]` and select one direction
   **plus (Blank)** — otherwise a point's Receipt and Delivery postings both
   plot, but flows-sourced rows (NGTL, CGT receipts, SoCal dailies) carry a
   blank direction and would vanish under a single-direction filter.
7. On `permian_ehrenberg` only: also filter `throughput[source]` to one of
   `capacity`/`flows` — `socal:el_paso_ehrenberg` is the one corridor point
   posted from both reports (scheduled vs actual — see the grain notes), so
   its tile would sum the two measures otherwise.
8. Keep **Format → Small multiples → Y-axis shared** so tiles are comparable.

> `wcsb_malin` is the one to look at first: it runs NGTL (metric, via `flows`)
> straight through GTN (Dth, via `capacity`) to CGT's actual border receipt —
> all three now in one line chart because `v_throughput` normalized them.

### 7.2 Ranked scheduled-vs-capacity bar (key points)
1. Canvas → **Visualizations → Stacked bar chart**.
2. **Y-axis:** `locations[display_name]`.
3. **X-axis (values):** measure `[Scheduled MMcfd]` then `[Headroom MMcfd]` — they
   stack to capacity (scheduled + headroom = capacity).
4. Filters pane → `locations[is_key_point]` **is 1**, and pick a single
   `throughput[flow_direction]`.
5. Sort by load: **⋯ → Sort axis → Scheduled MMcfd → Descending**.
6. Flag constrained points: **Format → Bars → Colors → fx (conditional)** on the
   `Scheduled MMcfd` series → **Format style: Rules** on `[Utilization %]`,
   e.g. `>= 0.9` → red.

Unit note: MMcf/d values are heat-content approximations (Dth ÷ 1030 at ~1030
Btu/cf; 10³m³ × 0.0353147) — right for corridor reasoning, not invoice math.

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
