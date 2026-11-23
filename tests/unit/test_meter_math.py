from decimal import Decimal
from pathlib import Path

import pytest

from ingest.meter_math import BadInput, MeterConfig, compute_intervals, iso, parse_csv

SAMPLE = Path(__file__).resolve().parents[2] / "sample-data" / "readings-2026-11-20.csv"
CFG = MeterConfig()


def csv(*rows):
    return "site_id,meter_id,timestamp,register_kwh\n" + "\n".join(",".join(r) for r in rows) + "\n"


def by_end(intervals):
    return {(i.site_id, i.meter_id, iso(i.end)): i for i in intervals}


def test_plain_deltas():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:00:00Z", "10"),
        ("S", "M", "2026-01-01T00:15:00Z", "12.5"),
        ("S", "M", "2026-01-01T00:30:00Z", "15"),
    )), CFG)
    assert [(i.kwh, i.quality) for i in ivs] == [(Decimal("2.5"), "OK"), (Decimal("2.5"), "OK")]


def test_rollover_near_register_max_is_wrapped():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:00:00Z", "99998.00"),
        ("S", "M", "2026-01-01T00:15:00Z", "1.01"),
    )), CFG)
    assert ivs[0].quality == "ROLLOVER"
    assert ivs[0].kwh == Decimal("3.00")  # 1.99 to the max, then 1.01 after the wrap
    assert not ivs[0].needs_alert


def test_drop_far_from_max_is_reset_with_unknown_energy():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:00:00Z", "45218.9"),
        ("S", "M", "2026-01-01T00:15:00Z", "0.4"),
    )), CFG)
    assert (ivs[0].kwh, ivs[0].quality, ivs[0].needs_alert) == (None, "RESET", True)


def test_spike_is_flagged():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:00:00Z", "100"),
        ("S", "M", "2026-01-01T00:15:00Z", "151"),
    )), CFG)
    assert ivs[0].quality == "SPIKE" and ivs[0].needs_alert


def test_gap_is_flagged_and_not_judged_as_a_spike():
    # 60 kWh over one hour is fine for a 50 kWh-per-15-min limit.
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:00:00Z", "100"),
        ("S", "M", "2026-01-01T01:00:00Z", "160"),
    )), CFG)
    assert (ivs[0].quality, ivs[0].kwh) == ("GAP", Decimal("60"))


def test_unsorted_input_and_duplicate_timestamps():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "M", "2026-01-01T00:30:00Z", "15"),
        ("S", "M", "2026-01-01T00:00:00Z", "10"),
        ("S", "M", "2026-01-01T00:15:00Z", "12"),
        ("S", "M", "2026-01-01T00:15:00Z", "12"),
    )), CFG)
    assert [i.kwh for i in ivs] == [Decimal("2"), Decimal("3")]


def test_meters_are_independent():
    ivs = compute_intervals(parse_csv(csv(
        ("S", "A", "2026-01-01T00:00:00Z", "1"),
        ("S", "B", "2026-01-01T00:00:00Z", "500"),
        ("S", "A", "2026-01-01T00:15:00Z", "2"),
        ("S", "B", "2026-01-01T00:15:00Z", "501"),
    )), CFG)
    assert {(i.meter_id, i.kwh) for i in ivs} == {("A", Decimal("1")), ("B", Decimal("1"))}


def test_naive_and_offset_timestamps_normalise_to_utc():
    rows = parse_csv(csv(("S", "M", "2026-01-01T01:00:00+01:00", "1"), ("S", "M", "2026-01-01T00:15:00", "2")))
    assert iso(rows[0].ts) == "2026-01-01T00:00:00Z"
    assert iso(rows[1].ts) == "2026-01-01T00:15:00Z"


def test_missing_column_is_rejected():
    with pytest.raises(BadInput, match="register_kwh"):
        parse_csv("site_id,meter_id,timestamp\nS,M,2026-01-01T00:00:00Z\n")


def test_bad_number_reports_line():
    with pytest.raises(BadInput, match="line 3"):
        parse_csv(csv(("S", "M", "2026-01-01T00:00:00Z", "1"), ("S", "M", "2026-01-01T00:15:00Z", "abc")))


def test_utf8_bom_header_is_accepted():
    assert len(parse_csv("﻿" + csv(("S", "M", "2026-01-01T00:00:00Z", "1")))) == 1


def test_config_from_dict_overrides_defaults():
    cfg = MeterConfig.from_dict({"max_interval_kwh": 5})
    assert cfg.max_interval_kwh == Decimal("5") and cfg.expected_interval_s == 900


def test_sample_file_matches_what_verify_expects():
    ivs = by_end(compute_intervals(parse_csv(SAMPLE.read_text()), CFG))
    assert len(ivs) == 13
    assert sorted(i.quality for i in ivs.values() if i.needs_alert) == ["GAP", "RESET", "SPIKE"]
    assert ivs[("LAG-OJODU-01", "INV-1", "2026-11-20T08:45:00Z")].kwh == Decimal("17.49")
