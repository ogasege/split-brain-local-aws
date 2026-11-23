"""Pure meter maths: no AWS calls here, so it is fully unit-testable.

Smart meters and inverters report a cumulative energy register (kWh), not
the energy used in an interval. To get interval energy you subtract
consecutive readings, and that is where real field data bites:

* ROLLOVER: the register hits its maximum (e.g. 99999.99) and wraps to 0.
* RESET:    the register drops for any other reason (meter swapped,
            firmware reset). The interval energy is unknown.
* SPIKE:    a delta that is physically implausible for the site.
* GAP:      readings missing between two timestamps.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

REQUIRED_COLUMNS = ("site_id", "meter_id", "timestamp", "register_kwh")

# Quality flags that should raise an operator alert.
ALERT_FLAGS = frozenset({"RESET", "SPIKE", "GAP"})


@dataclass(frozen=True)
class Reading:
    site_id: str
    meter_id: str
    ts: datetime
    register_kwh: Decimal


@dataclass(frozen=True)
class Interval:
    site_id: str
    meter_id: str
    start: datetime
    end: datetime
    kwh: Decimal | None  # None when the energy cannot be known (RESET)
    quality: str  # OK | ROLLOVER | RESET | SPIKE | GAP

    @property
    def needs_alert(self) -> bool:
        return self.quality in ALERT_FLAGS


@dataclass(frozen=True)
class MeterConfig:
    register_max_kwh: Decimal = Decimal("99999.99")
    rollover_window: Decimal = Decimal("0.05")  # prev must be in top 5% of range
    max_interval_kwh: Decimal = Decimal("50")
    expected_interval_s: int = 900  # 15 minute readings

    @classmethod
    def from_dict(cls, raw: dict) -> "MeterConfig":
        return cls(
            register_max_kwh=Decimal(str(raw.get("register_max_kwh", cls.register_max_kwh))),
            rollover_window=Decimal(str(raw.get("rollover_window", cls.rollover_window))),
            max_interval_kwh=Decimal(str(raw.get("max_interval_kwh", cls.max_interval_kwh))),
            expected_interval_s=int(raw.get("expected_interval_s", cls.expected_interval_s)),
        )


class BadInput(ValueError):
    """Raised for a file we refuse to process (wrong header, bad values)."""


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if ts.tzinfo is None:  # treat naive timestamps as UTC, never local time
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_csv(text: str) -> list[Reading]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise BadInput(f"missing columns: {', '.join(missing)}")
    readings = []
    for line_no, row in enumerate(reader, start=2):
        try:
            readings.append(
                Reading(
                    site_id=row["site_id"].strip(),
                    meter_id=row["meter_id"].strip(),
                    ts=_parse_ts(row["timestamp"]),
                    register_kwh=Decimal(row["register_kwh"].strip()),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report the line, not a stack trace
            raise BadInput(f"line {line_no}: {exc}") from exc
    return readings


def compute_intervals(readings: list[Reading], cfg: MeterConfig) -> list[Interval]:
    """Turn cumulative register readings into interval energy, per meter."""
    by_meter: dict[tuple[str, str], list[Reading]] = {}
    for r in readings:
        by_meter.setdefault((r.site_id, r.meter_id), []).append(r)

    out: list[Interval] = []
    rollover_floor = cfg.register_max_kwh * (1 - cfg.rollover_window)
    for (site, meter), rows in sorted(by_meter.items()):
        rows.sort(key=lambda r: r.ts)
        prev = None
        for cur in rows:
            if prev is not None and cur.ts == prev.ts:
                continue  # duplicate reading inside the file; keep the first
            if prev is None:
                prev = cur  # first reading is the baseline
                continue
            delta = cur.register_kwh - prev.register_kwh
            gap_s = (cur.ts - prev.ts).total_seconds()
            if delta < 0 and prev.register_kwh >= rollover_floor:
                kwh, quality = (cfg.register_max_kwh - prev.register_kwh) + cur.register_kwh, "ROLLOVER"
            elif delta < 0:
                kwh, quality = None, "RESET"
            elif gap_s > cfg.expected_interval_s * 1.5:
                kwh, quality = delta, "GAP"
            else:
                kwh, quality = delta, "OK"
            # A spike check applies to any interval energy we could compute,
            # scaled for gaps so a 1 hour gap is not judged as 15 minutes.
            if kwh is not None and quality in ("OK", "ROLLOVER"):
                periods = max(Decimal(1), Decimal(gap_s) / Decimal(cfg.expected_interval_s))
                if kwh > cfg.max_interval_kwh * periods:
                    quality = "SPIKE"
            out.append(Interval(site, meter, prev.ts, cur.ts, kwh, quality))
            prev = cur
    return out


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
