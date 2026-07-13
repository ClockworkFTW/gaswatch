"""Parser unit tests on saved fixture responses — no network access."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _client_returning(content: bytes, json_data=None):
    """EbbClient stand-in whose every get/post returns one canned response."""
    resp = MagicMock()
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace")
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = lambda: json.loads(content)
    client = MagicMock()
    client.get.return_value = resp
    client.post.return_value = resp
    client.archive.return_value = "archived"
    return client


def test_gtn_capacity_parses_locations():
    from gaswatch.pipelines.gtn import GtnAdapter

    oac = json.loads((FIXTURES / "gtn_oac.json").read_bytes())
    adapter = GtnAdapter()
    client = _client_returning((FIXTURES / "gtn_oac.json").read_bytes())
    # cycle lookup returns one cycle; capacity call returns the fixture
    cycles_resp = MagicMock()
    cycles_resp.json.return_value = [{"Key": "1", "Value": "Timely"}]
    cap_resp = MagicMock()
    cap_resp.json.return_value = oac
    cap_resp.content = (FIXTURES / "gtn_oac.json").read_bytes()
    client.get.side_effect = [cycles_resp, cap_resp]

    result = adapter.fetch_capacity(client, date(2026, 7, 11))
    assert len(result.capacity) > 10
    kingsgate = next(r for r in result.capacity if r.location_name == "KINGSGATE")
    assert kingsgate.design_cap == 3043102
    assert kingsgate.scheduled_qty == 2520579
    assert kingsgate.cycle == "timely"
    assert kingsgate.gas_day == "2026-07-11"


def test_gtn_notices_categorized_from_response():
    from gaswatch.pipelines.gtn import GtnAdapter

    payload = json.loads((FIXTURES / "gtn_notices.json").read_bytes())
    adapter = GtnAdapter()
    resp = MagicMock()
    resp.json.return_value = payload
    client = MagicMock()
    client.get.return_value = resp
    result = adapter.fetch_notices(client, date(2026, 7, 11))
    assert result.notices
    # fixture's first notice is a planned service outage despite indicator=1 request
    assert any(n.category == "planned_outage" for n in result.notices)
    assert all(n.notice_id for n in result.notices)


def test_ngtl_outages_to_notices():
    from gaswatch.pipelines.ngtl import NgtlAdapter

    adapter = NgtlAdapter()
    client = _client_returning((FIXTURES / "ngtl_outages.csv").read_bytes())
    result = adapter.fetch_outages(client, date(2026, 7, 11))
    assert len(result.notices) > 50
    first = result.notices[0]
    assert first.category == "maintenance"
    assert first.effective_start == "2026-07-05"
    assert first.extra["capability"] == 371000


def test_ngtl_flows_area_mapping():
    from gaswatch.pipelines.ngtl import NgtlAdapter

    adapter = NgtlAdapter()
    areas_resp = MagicMock()
    areas_resp.json.return_value = {"data": [{"id": 1, "acronym": "USJR"}]}
    chart_resp = MagicMock()
    chart_resp.json.return_value = json.loads((FIXTURES / "ngtl_chart.json").read_bytes())
    chart_resp.content = (FIXTURES / "ngtl_chart.json").read_bytes()
    csr_resp = MagicMock()
    csr_resp.json.return_value = json.loads((FIXTURES / "ngtl_csr.json").read_bytes())
    client = MagicMock()
    client.get.side_effect = [areas_resp, chart_resp, csr_resp]
    client.archive.return_value = "archived"

    result = adapter.fetch_flows(client, date(2026, 7, 11))
    usjr = [f for f in result.flows if f.area == "USJR"]
    assert usjr and usjr[0].flow == 351000.0
    snapshots = [f for f in result.flows if f.kind == "snapshot"]
    assert {"SYSTEM_RECEIPTS", "LINEPACK"} <= {f.area for f in snapshots}


def test_foothills_filters_to_foothills_areas():
    from gaswatch.pipelines.foothills import FoothillsAdapter

    adapter = FoothillsAdapter()
    client = _client_returning((FIXTURES / "ngtl_outages.csv").read_bytes())
    result = adapter.fetch_outages(client, date(2026, 7, 11))
    assert all(n.extra["area"] in {"FHBC", "FHSK", "FHZ8", "FHZ9"} for n in result.notices)


def test_cgt_interactivemap_capacity():
    from gaswatch.pipelines.cgt import CgtAdapter

    adapter = CgtAdapter()
    client = _client_returning((FIXTURES / "cgt_map.json").read_bytes())
    result = adapter.fetch_capacity(client, date(2026, 7, 11))
    # live pull uses the same canonical slugs as the XLSX backfill
    redwood = next(r for r in result.capacity if r.location_id == "redwood_path")
    assert redwood.scheduled_qty == 1383473
    assert redwood.operating_cap == 2094840
    assert redwood.cycle in ("timely", "evening", "id1", "id2", "id3", "current")
    assert any(r.location_id == "freemont_peak_receipts" for r in result.capacity)


def test_cgt_ofo_archive():
    from gaswatch.pipelines.cgt import CgtAdapter

    adapter = CgtAdapter()
    client = _client_returning((FIXTURES / "cgt_ofo.json").read_bytes())
    result = adapter.fetch_ofo(client, date(2026, 7, 11))
    assert len(result.notices) > 3
    first = result.notices[0]
    assert first.category == "ofo"
    assert first.extra["stage"] == 2
    assert first.notice_id.startswith("2026-07-10")


def test_cgt_foghorn_maintenance():
    from gaswatch.pipelines.cgt import CgtAdapter

    adapter = CgtAdapter()
    client = _client_returning((FIXTURES / "cgt_foghorn.html").read_bytes())
    result = adapter.fetch_maintenance(client, date(2026, 7, 11))
    assert len(result.notices) >= 4
    assert any("Burney" in n.body_text for n in result.notices)
    assert all(n.category == "maintenance" for n in result.notices)


def test_epng_segment_grid_parse():
    from gaswatch.pipelines.epng import EpngAdapter

    adapter = EpngAdapter()
    client = _client_returning((FIXTURES / "epng_seg0.html").read_bytes())
    # paging POST returns an empty page so pagination stops after page 1
    empty = MagicMock()
    empty.text = "<html><body></body></html>"
    client.post.return_value = empty
    result = adapter.fetch_capacity(client, date(2026, 7, 11))
    assert len(result.capacity) == 75  # fixture holds page 1 only
    bondad = next(r for r in result.capacity if r.location_name == "BONDADST")
    assert bondad.design_cap == 707199
    assert bondad.operating_cap == 726500
    assert bondad.location_type == "segment"


def test_epng_notices_grid_parse():
    from gaswatch.pipelines.epng import EpngAdapter

    adapter = EpngAdapter()
    client = _client_returning((FIXTURES / "epng_notices.html").read_bytes())
    result = adapter.fetch_notices(client, date(2026, 7, 11))
    crits = [n for n in result.notices if n.category == "critical"]
    assert crits
    assert all(n.notice_id.isdigit() for n in result.notices)


def test_db_upserts_are_idempotent(tmp_path):
    from gaswatch import db as dbm
    from gaswatch.models import CapacityRecord

    conn = dbm.connect(tmp_path / "t.db")
    rec = CapacityRecord(pipeline="x", gas_day="2026-07-11", cycle="timely",
                         location_type="point", location_id="1", available_cap=5.0)
    dbm.upsert_capacity(conn, [rec])
    rec.available_cap = 7.0
    dbm.upsert_capacity(conn, [rec])
    rows = conn.execute("SELECT COUNT(*), MAX(available_cap) FROM capacity").fetchone()
    assert rows == (1, 7.0)


def test_transwestern_oac_csv_parse():
    from gaswatch.pipelines.transwestern import TranswesternAdapter

    adapter = TranswesternAdapter()
    client = _client_returning((FIXTURES / "tw_oac.csv").read_bytes())
    result = adapter.fetch_capacity(client, date(2026, 7, 11))
    # same CSV returned for all four cycle requests in the mock
    timely = [r for r in result.capacity if r.cycle == "timely"]
    assert len(timely) > 50
    needles = next(r for r in timely if r.location_name == "SOCAL NEEDLES")
    assert needles.gas_day == "2026-07-11"  # stamped from request, not CSV
    assert needles.design_cap == 850000
    assert needles.scheduled_qty is not None


def test_kernriver_daily_report_parse():
    from gaswatch.pipelines.kernriver import KernRiverAdapter

    adapter = KernRiverAdapter()
    client = _client_returning((FIXTURES / "kr_home.html").read_bytes())
    result = adapter.fetch_capacity(client, date(2026, 7, 11))
    assert len(result.capacity) >= 30  # ~12 constraint points x 3 gas days
    daggett = [r for r in result.capacity if r.location_id == "024011"]
    assert daggett and daggett[0].location_name.startswith("Daggett")
    assert all(r.cycle in ("timely", "evening", "id1", "id2", "id3")
               for r in result.capacity)
    muddy = [r for r in result.capacity if r.location_id == "054001"]
    assert muddy and muddy[0].operating_cap == 2300000


def test_socal_capacity_csv_parse():
    from gaswatch.pipelines.socal import SocalAdapter

    adapter = SocalAdapter()
    client = _client_returning((FIXTURES / "socal_capacity.csv").read_bytes())
    result = adapter.fetch_capacity(client, date(2026, 7, 10))
    # the export must be POSTed with the gas day in the form body — a GET is
    # silently answered with the current-day snapshot whatever the params say
    assert not client.get.called
    for call in client.post.call_args_list:
        assert call.kwargs["data"]["gasFlowDate"] == "07/10/2026"
        assert call.kwargs["data"]["HiddenGasFlowDateField"] == "07/10/2026"
    timely = [r for r in result.capacity if r.cycle == "timely"]
    ehrenberg = next(r for r in timely if r.location_id == "el_paso_ehrenberg")
    assert ehrenberg.location_type == "point"  # indented child row
    assert ehrenberg.operating_cap == 1245582
    zones = [r for r in timely if r.location_type == "zone"]
    assert any(r.location_id == "southern_zone" for r in zones)


def test_socal_dailyops_csv_parse():
    from gaswatch.pipelines.socal import SocalAdapter

    adapter = SocalAdapter()
    client = _client_returning((FIXTURES / "socal_dailyops.csv").read_bytes())
    result = adapter.fetch_flows(client, date(2026, 7, 10))
    kinds = {f.kind for f in result.flows}
    assert {"actual", "estimate", "forecast"} <= kinds
    ehr = [f for f in result.flows if f.area == "el_paso_ehrenberg" and f.kind == "actual"]
    assert ehr and ehr[0].flow == 514000
    assert ehr[0].gas_day == "2026-07-09"  # actual column is the prior gas day


def test_date_normalization():
    from gaswatch.dates import month_day_range, to_iso

    assert to_iso("07/10/2026 8:06:48PM") == "2026-07-10 20:06"
    assert to_iso("07/10/2026 17:30") == "2026-07-10 17:30"
    assert to_iso("4/23/2026 12:54:01 PM") == "2026-04-23 12:54"
    assert to_iso("2026-07-05") == "2026-07-05"
    assert to_iso("12/31/9000") == ""  # open-ended sentinel
    assert to_iso("&nbsp;   &nbsp;") == ""
    assert to_iso("") == ""
    assert to_iso("July 06 - 10") == "July 06 - 10"  # unparseable kept as-is
    assert month_day_range("July 06 - 10", 2026) == ("2026-07-06", "2026-07-10")
    assert month_day_range("July 20", 2026) == ("2026-07-20", "2026-07-20")
    assert month_day_range("December 28 - January 03", 2026) == ("2026-12-28", "2027-01-03")


def test_epng_duplicate_segment_numbers_kept_distinct(tmp_path):
    """EPNG reuses segment numbers across named points — the widened capacity
    key must keep them as separate rows."""
    from gaswatch import db as dbm
    from gaswatch.models import CapacityRecord

    conn = dbm.connect(tmp_path / "t.db")
    rows = [
        CapacityRecord(pipeline="epng", gas_day="2026-07-11", cycle="evening",
                       location_type="segment", location_id="540",
                       location_name="N PHX1", available_cap=18891.0),
        CapacityRecord(pipeline="epng", gas_day="2026-07-11", cycle="evening",
                       location_type="segment", location_id="540",
                       location_name="MARICOPA", available_cap=0.0),
    ]
    dbm.upsert_capacity(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM capacity").fetchone()[0] == 2


def test_rate_doc_change_detection(tmp_path):
    from gaswatch import db as dbm
    from gaswatch.models import RateDoc

    conn = dbm.connect(tmp_path / "t.db")
    doc = RateDoc(pipeline="x", doc_type="tariff", title="t", url="u", content_hash="a")
    assert dbm.upsert_rate_docs(conn, [doc])  # new -> changed
    assert not dbm.upsert_rate_docs(conn, [doc])  # same hash -> unchanged
    doc.content_hash = "b"
    assert dbm.upsert_rate_docs(conn, [doc])  # new hash -> changed


# -- currently effective rates view ----------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("TW Rate Matrix Effective 4/1/2023", "2023-04-01"),
    ("TW Rate Matrix Effective 10/1/2009 -- PHX", "2009-10-01"),
    ("Rate Matrix - Effective 8/01/2023", "2023-08-01"),
    ("Index Rates 2026-06", "2026-06-01"),
    ("Index Rates 2012-09 - REVISED", "2012-09-01"),
    ("2026 Final Attachment 2 Delivery Point Rates - Amended Effective July 1, 2026",
     "2026-07-01"),
    ("TW ACA Notice October 2025", "2025-10-01"),
    ("TW ACA Notice October, 2020", "2020-10-01"),
    ("rates2026", "2026-01-01"),
    ("2017 Table of Effective Rates", "2017-01-01"),
    ("Rate Schedule FT-R", None),
    ("General Terms and Conditions", None),
    ("Appendix H - Appendix H – CO2 Management Service", None),
])
def test_parse_effective(title, expected):
    from gaswatch.rates import parse_effective

    assert parse_effective(title)[0] == expected


def test_parse_effective_series_key_groups_versions():
    from gaswatch.rates import parse_effective

    k1 = parse_effective("TW Rate Matrix Effective 4/1/2023")[1]
    k2 = parse_effective("TW Rate Matrix Effective 10/1/2014")[1]
    k_phx = parse_effective("TW Rate Matrix Effective 10/1/2014 - PHX")[1]
    assert k1 == k2
    assert k_phx != k1
    # amended posting belongs to the same series as the original
    a1 = parse_effective("Attachment 2 Delivery Point Rates - Effective June 1, 2026")[1]
    a2 = parse_effective("Attachment 2 Delivery Point Rates - Amended Effective July 1, 2026")[1]
    assert a1 == a2


def test_effective_rate_docs_selection(tmp_path):
    from datetime import date

    from gaswatch import db as dbm
    from gaswatch.models import RateDoc
    from gaswatch.rates import effective_rate_docs

    conn = dbm.connect(tmp_path / "t.db")
    docs = [
        RateDoc(pipeline="tw", doc_type="rates", title="Rate Matrix Effective 4/1/2023", url="u1"),
        RateDoc(pipeline="tw", doc_type="rates", title="Rate Matrix Effective 6/1/2026", url="u2"),
        RateDoc(pipeline="tw", doc_type="rates", title="Rate Matrix Effective 9/1/2026", url="u3"),
        RateDoc(pipeline="tw", doc_type="tariff", title="Entire tariff (PDF)", url="u4"),
    ]
    dbm.upsert_rate_docs(conn, docs)
    rows = effective_rate_docs(conn, today=date(2026, 7, 12))
    by_url = {r["url"]: r for r in rows}
    assert "u1" not in by_url                       # superseded, hidden by default
    assert by_url["u2"]["status"] == "current"
    assert by_url["u3"]["status"] == "pending"      # future-dated
    assert by_url["u4"]["status"] == "current"      # undated standing doc
    rows_all = effective_rate_docs(conn, today=date(2026, 7, 12), include_superseded=True)
    assert {r["url"]: r["status"] for r in rows_all}["u1"] == "superseded"

    # a doc missing from the latest pull (stale last_seen) drops out of the view
    conn.execute("UPDATE rate_docs SET last_seen='2020-01-01T00:00:00Z' WHERE url='u4'")
    rows = effective_rate_docs(conn, today=date(2026, 7, 12))
    assert "u4" not in {r["url"] for r in rows}


def test_epng_rates_parses_cer_sections():
    from gaswatch.pipelines.epng import EpngAdapter

    page = MagicMock()
    page.text = (FIXTURES / "epng_cer.html").read_text(encoding="utf-8")
    probe = MagicMock()
    probe.headers = {"Content-Range": "bytes 0-0/47500000", "Last-Modified": "x"}
    client = MagicMock()
    client.get.side_effect = [page, probe]
    result = EpngAdapter().fetch_rates(client, date(2026, 7, 12))
    titles = {d.title for d in result.rate_docs}
    assert "Production Area Rates" in titles
    assert "Statement of Negotiated Rates" in titles
    cer = next(d for d in result.rate_docs if d.title == "Production Area Rates")
    assert cer.url.endswith("EPNG_EntireTariff.pdf#cerpasr")
    # the entire-tariff PDF is still tracked alongside the CER sections
    assert any(d.doc_type == "tariff" for d in result.rate_docs)


# -- parsed tariff rate values (ratesheets) ---------------------------------------


def _pages(fixture: str) -> dict[int, str]:
    """Rebuild a {page: text} dict from an @@@PAGE n@@@-delimited fixture."""
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    out = {}
    for chunk in raw.split("@@@PAGE ")[1:]:
        num, _, text = chunk.partition("@@@\n")
        out[int(num)] = text
    return out


def _find(rates, **want):
    return [r for r in rates
            if all(getattr(r, k) == v for k, v in want.items())]


def test_parse_cgt_ratesheet():
    from gaswatch.ratesheets import parse_cgt

    rates = parse_cgt(_pages("cgt_ratesheet.txt")[0])
    assert len(rates) > 80
    mfv = _find(rates, rate_schedule="G-AFT", component="reservation",
                path="Redwood to On-System Core (MFV)")
    assert [r.value for r in mfv] == [12.2199]
    sfv = _find(rates, rate_schedule="G-AFT", component="reservation",
                path="Redwood to On-System Core (SFV)")
    assert [r.value for r in sfv] == [19.5508]
    assert all(r.effective_date == "2026-01-01" for r in rates)
    # storage schedule values survive the column split
    assert _find(rates, rate_schedule="G-NFS", component="withdrawal")[0].value == 26.1629


def test_parse_epng_statement_of_rates():
    from gaswatch.ratesheets import parse_epng

    rates = parse_epng(_pages("epng_rate_pages.txt"))
    assert len(rates) > 250
    ca = _find(rates, rate_schedule="FT-1", component="reservation",
               path="California (monthly)")
    assert [r.value for r in ca] == [9.5660]
    # per-schedule daily max reservation, Production Area
    pa = _find(rates, rate_schedule="FTH-8", component="reservation",
               path="Production Area", qualifier="max")
    assert [r.value for r in pa] == [0.1532]
    # interruptible zonal usage
    it = _find(rates, rate_schedule="IT-1", component="usage", path="Arizona",
               qualifier="max")
    assert [r.value for r in it] == [0.2962]
    # fuel: mainline total retention
    fuel = _find(rates, component="fuel", path="Mainline Fuel")
    assert [r.value for r in fuel] == [2.08]
    # truncated Article 11.2(B) rows must not leak through
    assert all(len(_find(rates, rate_schedule=s, component="reservation",
                         path="Production Area (monthly)")) == 1
               for s in ("FT-1", "FTH-8"))


def test_parse_kern_statement_of_rates():
    from gaswatch.ratesheets import parse_kern

    rates = parse_kern(_pages("kern_rate_pages.txt"))
    rec = _find(rates, component="reservation", qualifier="max",
                path="Recourse - Original System/2002 Expansion Project")
    assert [r.value for r in rec] == [0.4734]
    fifteen = _find(rates, component="reservation", qualifier="max",
                    path="15-Year - Original System/2002 Expansion Project")
    assert [r.value for r in fifteen] == [0.3633]
    # period-two (shipper-specific) sheets are skipped
    assert not any("Period" in r.path for r in rates)
    assert all(r.rate_schedule.startswith("KR") for r in rates)


def test_parse_gtn_statement_of_rates():
    from gaswatch.ratesheets import parse_gtn

    rates = parse_gtn(_pages("gtn_rate_pages.txt"))
    base = _find(rates, rate_schedule="FTS-1/LFS-1/FHS", component="reservation",
                 path="non-mileage", qualifier="max")
    assert [r.value for r in base] == [0.0254384]
    assert _find(rates, component="fuel")[0].value == 0.005


def test_parse_nwp_statement_of_rates():
    from gaswatch.ratesheets import parse_nwp

    rates = parse_nwp(_pages("nwp_rate_pages.txt"))
    res = _find(rates, rate_schedule="TF-1", component="reservation",
                path="Large Customer System-Wide", qualifier="max")
    assert [r.value for r in res] == [0.3725]
    ti = _find(rates, rate_schedule="TI-1", component="usage", qualifier="max")
    assert [r.value for r in ti] == [0.38185]


def test_parse_tw_rate_matrix():
    from gaswatch.ratesheets import parse_tw

    rates = parse_tw(_pages("tw_rate_matrix.txt"))
    assert all(r.effective_date == "2023-08-01" for r in rates)

    def one(**want):
        vals = {r.value for r in _find(rates, **want)}
        assert len(vals) == 1, (want, vals)
        return vals.pop()

    assert one(rate_schedule="FTS-1", component="reservation",
               path="West Of Thoreau -> Phoenix") == 0.6561
    # rows missing their own-column cell still map correctly
    assert one(rate_schedule="FTS-1", component="reservation",
               path="Thoreau Point -> EOT") == 0.2765
    assert one(rate_schedule="FTS-1", component="reservation",
               path="Phoenix -> Phoenix") == 0.5
    assert one(rate_schedule="ITS-1", component="usage",
               path="Ash Fork Point -> San Juan (Blanco)") == 0.3158
    assert one(component="fuel", path="East Of Thoreau -> Phoenix") == 2.63
    # second page carries the FTS-3/FTS-5 matrix
    assert _find(rates, rate_schedule="FTS-3")


def test_parse_ngtl_table_of_rates():
    from gaswatch.ratesheets import parse_ngtl

    rates = parse_ngtl(_pages("ngtl_tolls.txt")[0])
    assert _find(rates, rate_schedule="FT-R", component="demand")[0].value == 318.99
    g2 = _find(rates, rate_schedule="FT-D", component="demand",
               path="Group 2 delivery")
    assert [r.value for r in g2] == [9.42]
    assert all(r.effective_date == "2026-06-01" for r in rates)


def test_tariff_rates_current_view(tmp_path):
    from gaswatch import db as dbm
    from gaswatch.models import TariffRate

    conn = dbm.connect(tmp_path / "t.db")
    mk = lambda eff, val: TariffRate(
        pipeline="x", rate_schedule="FT", component="reservation", path="a",
        qualifier="max", value=val, unit="$/Dth", effective_date=eff)
    dbm.upsert_tariff_rates(conn, [mk("2020-01-01", 1.0), mk("2025-01-01", 2.0),
                                   mk("2099-01-01", 9.0)])
    rows = conn.execute("SELECT value FROM v_current_tariff_rates").fetchall()
    assert rows == [(2.0,)]  # newest in-force filing wins; future filing excluded
    # idempotent upsert refreshes in place
    dbm.upsert_tariff_rates(conn, [mk("2025-01-01", 2.5)])
    rows = conn.execute("SELECT value FROM v_current_tariff_rates").fetchall()
    assert rows == [(2.5,)]


def test_parse_foothills_table_of_rates():
    from gaswatch.ratesheets import parse_foothills

    rates = parse_foothills(_pages("foothills_rates.txt")[0])
    assert len(rates) == 10
    z6 = _find(rates, rate_schedule="FT", component="demand", path="Zone 6")
    assert [r.value for r in z6] == [0.0064729237]
    assert z6[0].unit == "$/GJ/Km/Month"
    # effective date survives the intra-word spacing artifact ("J anuary")
    assert all(r.effective_date == "2026-01-01" for r in rates)
    assert len(_find(rates, rate_schedule="SYSTEM", component="surcharge")) == 2


def test_parse_ruby_statement_of_rates():
    from gaswatch.ratesheets import parse_ruby

    rates = parse_ruby(_pages("ruby_rate_pages.txt"))
    lt = _find(rates, rate_schedule="FT", component="reservation", path="long-term",
               qualifier="max")
    assert [r.value for r in lt] == [34.5826]
    # three-column commodity row maps FT/IT/SS-1 in order
    it = _find(rates, rate_schedule="IT", component="usage", path="long-term",
               qualifier="max")
    assert [r.value for r in it] == [1.1469]
    # short-term peak options are distinct paths
    pk4 = _find(rates, rate_schedule="FT", component="reservation",
                path="short-term 4 month peak option peak", qualifier="max")
    assert [r.value for r in pk4] == [51.874]
    off1 = _find(rates, rate_schedule="FT", component="reservation",
                 path="short-term 1 month peak option off-peak", qualifier="max")
    assert [r.value for r in off1] == [33.0107]
    assert all(r.effective_date == "2026-06-01" for r in rates)


def test_parse_socal_gbts():
    from gaswatch.ratesheets import parse_socal

    rates = parse_socal(_pages("socal_gbts.txt"))
    r1 = _find(rates, rate_schedule="G-BTS1", component="reservation")
    assert [r.value for r in r1] == [0.8679]
    mfv = _find(rates, rate_schedule="G-BTS2", path="modified fixed variable")
    assert {r.component: r.value for r in mfv} == {"reservation": 0.69432,
                                                   "usage": 0.17358}
    # market-based rows (caps, not rates) are excluded
    assert not _find(rates, rate_schedule="G-BTSN1")
    # effective date comes from the rates sheet, not other sheets in the PDF
    assert all(r.effective_date == "2026-07-01" for r in rates)


def test_ratesheet_validation_catches_layout_changes():
    from gaswatch.models import TariffRate
    from gaswatch.ratesheets import parse_foothills, validated

    good = _pages("foothills_rates.txt")[0]
    rates = validated("foothills", parse_foothills(good))  # passes

    # too few records
    with pytest.raises(RuntimeError, match="only 3 values"):
        validated("foothills", rates[:3])
    # missing required component
    with pytest.raises(RuntimeError, match="missing expected"):
        validated("foothills", [r for r in rates if r.component != "demand"]
                  + [TariffRate(pipeline="foothills", rate_schedule="X",
                                component="usage", path=str(i),
                                effective_date="2026-01-01", value=1.0)
                     for i in range(6)])
    # out-of-range value
    bad = [TariffRate(pipeline="socal", rate_schedule="G-BTS1", component="reservation",
                      path="p", value=5000.0, unit="$/Dth/d",
                      effective_date="2026-01-01")] * 5
    with pytest.raises(RuntimeError, match="out-of-range"):
        validated("socal", bad)
    # missing effective date
    with pytest.raises(RuntimeError, match="no effective date"):
        validated("socal", [TariffRate(pipeline="socal", rate_schedule="G-BTS1",
                                       component="reservation", path=str(i), value=0.5)
                            for i in range(5)])


def test_export_powerbi_writes_csv_set(tmp_path):
    from typer.testing import CliRunner

    from gaswatch import db as dbm
    from gaswatch.cli import app
    from gaswatch.models import CapacityRecord, RateDoc, TariffRate

    db = tmp_path / "t.db"
    conn = dbm.connect(db)
    dbm.upsert_capacity(conn, [CapacityRecord(
        pipeline="gtn", gas_day="2026-07-01", cycle="timely", location_type="point",
        location_id="3500", location_name="Kingsgate", operating_cap=100.0,
        scheduled_qty=80.0)])
    dbm.upsert_tariff_rates(conn, [TariffRate(
        pipeline="gtn", rate_schedule="FTS-1", component="reservation", path="p",
        qualifier="max", value=0.02, unit="$/Dth/d", effective_date="2026-04-01")])
    dbm.upsert_rate_docs(conn, [RateDoc(pipeline="gtn", doc_type="rates",
                                        title="GTN rates", url="u")])
    conn.close()

    out = tmp_path / "pbi"
    result = CliRunner().invoke(app, ["export-powerbi", "--db", str(db),
                                      "--out-dir", str(out)])
    assert result.exit_code == 0, result.output
    expected = {"capacity_daily", "locations", "flows", "notices",
                "tariff_rates_current", "tariff_rates_history", "pull_health",
                "rate_docs_current", "feed"}
    assert {p.stem for p in out.glob("*.csv")} == expected
    cap = (out / "capacity_daily.csv").read_text(encoding="utf-8-sig").splitlines()
    assert cap[0].startswith("location_key,pipeline,gas_day")
    assert cap[0].endswith("scheduled_mmcfd,capacity_mmcfd")
    assert cap[1].startswith("gtn:3500,gtn,2026-07-01")
    assert ",0.8," in cap[1]  # utilization column
    locs = (out / "locations.csv").read_text(encoding="utf-8-sig").splitlines()
    assert locs[0].endswith("corridor,position")
    gtn_row = next(l for l in locs if l.startswith("gtn:3500"))
    assert gtn_row.endswith("north,3")
