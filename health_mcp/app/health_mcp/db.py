"""SQLite storage for Health Auto Export payloads.

Design notes
------------
* One row per (metric, timestamp). Re-sent data is upserted, so the phone
  can safely re-send overlapping windows.
* `ts` keeps the local timestamp *with offset* exactly as Apple reports it
  ("2026-10-25 07:00:00 +0100"). Day/hour buckets use the local date, which
  is what a human means by "yesterday's steps". `ts_utc` is for ordering.
* `qty` is the primary numeric value: `qty` for simple metrics, `Avg` for
  min/avg/max metrics such as heart_rate, `asleep` for sleep_analysis.
  Everything the app sent is kept verbatim in `fields` (JSON).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("health_mcp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    metric   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,
    qty      REAL,
    units    TEXT,
    source   TEXT,
    fields   TEXT NOT NULL,
    PRIMARY KEY (metric, ts_utc)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, ts);

CREATE TABLE IF NOT EXISTS workouts (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    start        TEXT,
    "end"        TEXT,
    start_utc    TEXT,
    end_utc      TEXT,
    duration_s   REAL,
    active_kcal  REAL,
    distance_km  REAL,
    avg_hr       REAL,
    max_hr       REAL,
    fields       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts (start_utc);

CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY,
    received_utc  TEXT NOT NULL,
    bytes         INTEGER,
    metrics       INTEGER,
    samples       INTEGER,
    workouts      INTEGER,
    error         TEXT
);
"""

# Metric families where the "primary" value is not `qty`.
PRIMARY_FIELD_FALLBACKS = ("qty", "Avg", "avg", "asleep", "totalSleep", "value", "systolic")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",   # Health Auto Export default
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_date(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_local_iso(dt: datetime) -> str:
    """Local wall-clock time with offset, e.g. 2026-10-25T07:00:00+01:00."""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.isoformat(timespec="seconds")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict) and "qty" in value:
        return _num(value["qty"])
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _primary_value(fields: dict[str, Any]) -> float | None:
    for key in PRIMARY_FIELD_FALLBACKS:
        if key in fields:
            v = _num(fields[key])
            if v is not None:
                return v
    return None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ ingest
    def ingest(self, payload: dict[str, Any], nbytes: int = 0) -> dict[str, int]:
        data = payload.get("data", payload)
        metrics = data.get("metrics") or []
        workouts = data.get("workouts") or []

        n_samples = 0
        n_workouts = 0
        with self.conn:  # one transaction for the whole batch
            self.conn.execute("BEGIN")
            for metric in metrics:
                n_samples += self._ingest_metric(metric)
            for workout in workouts:
                n_workouts += self._ingest_workout(workout)
            self.conn.execute(
                "INSERT INTO ingest_log (received_utc, bytes, metrics, samples, workouts) VALUES (?,?,?,?,?)",
                (_to_utc_iso(datetime.now(timezone.utc)), nbytes, len(metrics), n_samples, n_workouts),
            )
        log.info("ingested %d samples across %d metrics, %d workouts", n_samples, len(metrics), n_workouts)
        return {"metrics": len(metrics), "samples": n_samples, "workouts": n_workouts}

    def log_ingest_error(self, nbytes: int, error: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO ingest_log (received_utc, bytes, metrics, samples, workouts, error) VALUES (?,?,0,0,0,?)",
                (_to_utc_iso(datetime.now(timezone.utc)), nbytes, error[:500]),
            )

    def _ingest_metric(self, metric: dict[str, Any]) -> int:
        name = metric.get("name")
        if not name:
            return 0
        units = metric.get("units")
        rows: list[tuple] = []
        for point in metric.get("data") or []:
            if not isinstance(point, dict):
                continue
            dt = parse_date(point.get("date") or point.get("start") or "")
            if dt is None:
                continue
            fields = {k: v for k, v in point.items() if k != "date"}
            rows.append((
                name,
                _to_local_iso(dt),
                _to_utc_iso(dt),
                _primary_value(fields),
                units,
                point.get("source"),
                json.dumps(fields, separators=(",", ":"), ensure_ascii=False),
            ))
        if rows:
            self.conn.executemany(
                """INSERT INTO samples (metric, ts, ts_utc, qty, units, source, fields)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(metric, ts_utc) DO UPDATE SET
                     ts=excluded.ts, qty=excluded.qty, units=excluded.units,
                     source=excluded.source, fields=excluded.fields""",
                rows,
            )
        return len(rows)

    def _ingest_workout(self, w: dict[str, Any]) -> int:
        start = parse_date(w.get("start") or "")
        end = parse_date(w.get("end") or "")
        if start is None:
            return 0
        name = w.get("name") or "Workout"
        wid = w.get("id") or hashlib.sha1(f"{name}|{_to_utc_iso(start)}".encode()).hexdigest()[:16]

        hr_samples = w.get("heartRateData") or []
        avg_hr = _num(w.get("avgHeartRate"))
        max_hr = _num(w.get("maxHeartRate"))
        if hr_samples and (avg_hr is None or max_hr is None):
            avgs = [v for v in (_num(s.get("Avg")) for s in hr_samples if isinstance(s, dict)) if v is not None]
            maxs = [v for v in (_num(s.get("Max")) for s in hr_samples if isinstance(s, dict)) if v is not None]
            if avgs and avg_hr is None:
                avg_hr = sum(avgs) / len(avgs)
            if maxs and max_hr is None:
                max_hr = max(maxs)

        distance_km = None
        dist = w.get("distance")
        if isinstance(dist, dict):
            q = _num(dist.get("qty"))
            u = str(dist.get("units") or "km").lower()
            if q is not None:
                distance_km = q / 1000 if u in ("m", "meter", "meters") else q * 1.609344 if u in ("mi", "mile", "miles") else q

        duration = _num(w.get("duration"))
        if duration is None and end is not None:
            duration = (end - start).total_seconds()

        # Keep everything except the bulky per-second arrays.
        fields = {k: v for k, v in w.items() if k not in ("heartRateData", "heartRateRecovery", "route", "stepCount")}
        fields["_hr_samples"] = len(hr_samples)
        fields["_route_points"] = len(w.get("route") or [])

        self.conn.execute(
            """INSERT INTO workouts (id, name, start, "end", start_utc, end_utc, duration_s, active_kcal,
                                     distance_km, avg_hr, max_hr, fields)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, start=excluded.start, "end"=excluded."end",
                 start_utc=excluded.start_utc, end_utc=excluded.end_utc, duration_s=excluded.duration_s,
                 active_kcal=excluded.active_kcal, distance_km=excluded.distance_km,
                 avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, fields=excluded.fields""",
            (
                wid, name,
                _to_local_iso(start), _to_local_iso(end) if end else None,
                _to_utc_iso(start), _to_utc_iso(end) if end else None,
                duration,
                _num(w.get("activeEnergyBurned")) or _num(w.get("activeEnergy")),
                distance_km, avg_hr, max_hr,
                json.dumps(fields, separators=(",", ":"), ensure_ascii=False, default=str),
            ),
        )
        return 1

    # ------------------------------------------------------------------- query
    def list_metrics(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT metric, units, COUNT(*) AS samples, MIN(ts) AS first, MAX(ts) AS last
               FROM samples GROUP BY metric ORDER BY metric"""
        )
        return [dict(r) for r in cur]

    def latest(self, metric: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT metric, ts, qty, units, source, fields FROM samples WHERE metric=? ORDER BY ts_utc DESC LIMIT 1",
            (metric,),
        ).fetchone()
        return self._row_with_fields(row) if row else None

    def query(self, metric: str, start: str, end: str, aggregate: str, limit: int) -> list[dict[str, Any]]:
        start_iso, end_iso = _day_bounds(start, end)
        if aggregate == "raw":
            cur = self.conn.execute(
                """SELECT metric, ts, qty, units, source, fields FROM samples
                   WHERE metric=? AND ts >= ? AND ts < ? ORDER BY ts_utc LIMIT ?""",
                (metric, start_iso, end_iso, limit),
            )
            return [self._row_with_fields(r) for r in cur]

        bucket = {
            "hourly": "substr(ts, 1, 13)",
            "daily": "substr(ts, 1, 10)",
            "weekly": "strftime('%Y-W%W', substr(ts, 1, 10))",
            "monthly": "substr(ts, 1, 7)",
        }[aggregate]
        cur = self.conn.execute(
            f"""SELECT {bucket} AS bucket, COUNT(*) AS samples,
                       SUM(qty) AS sum, AVG(qty) AS avg, MIN(qty) AS min, MAX(qty) AS max,
                       MIN(units) AS units
                FROM samples WHERE metric=? AND ts >= ? AND ts < ?
                GROUP BY bucket ORDER BY bucket LIMIT ?""",
            (metric, start_iso, end_iso, limit),
        )
        return [dict(r) for r in cur]

    def daily_summary(self, day: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT metric, COUNT(*) AS samples, SUM(qty) AS sum, AVG(qty) AS avg,
                      MIN(qty) AS min, MAX(qty) AS max, MIN(units) AS units
               FROM samples WHERE substr(ts, 1, 10) = ?
               GROUP BY metric ORDER BY metric""",
            (day,),
        )
        return [dict(r) for r in cur]

    def workouts(self, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        start_iso, end_iso = _day_bounds(start, end)
        cur = self.conn.execute(
            """SELECT id, name, start, "end", duration_s, active_kcal, distance_km, avg_hr, max_hr, fields
               FROM workouts WHERE start >= ? AND start < ? ORDER BY start_utc DESC LIMIT ?""",
            (start_iso, end_iso, limit),
        )
        return [self._row_with_fields(r) for r in cur]

    def sql(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = query.strip().rstrip(";")
        head = q.lstrip("(").split(None, 1)[0].upper() if q else ""
        if head not in ("SELECT", "WITH", "EXPLAIN"):
            raise ValueError("Only read-only SELECT / WITH queries are allowed")
        ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            ro.execute("PRAGMA query_only=1")
            cur = ro.execute(q)
            return [dict(r) for r in cur.fetchmany(limit)]
        finally:
            ro.close()

    def ingest_status(self, limit: int = 10) -> dict[str, Any]:
        last = [dict(r) for r in self.conn.execute(
            "SELECT received_utc, bytes, metrics, samples, workouts, error FROM ingest_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )]
        totals = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM samples) AS samples, (SELECT COUNT(*) FROM workouts) AS workouts, "
            "(SELECT COUNT(DISTINCT metric) FROM samples) AS metrics"
        ).fetchone()
        return {"totals": dict(totals), "db_bytes": self.path.stat().st_size if self.path.exists() else 0, "recent": last}

    @staticmethod
    def _row_with_fields(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["fields"] = json.loads(d.get("fields") or "{}")
        except json.JSONDecodeError:
            pass
        return d


def _day_bounds(start: str, end: str) -> tuple[str, str]:
    """Accept YYYY-MM-DD or full ISO timestamps; `end` is exclusive.

    A bare end date is bumped by one day so "2026-10-01 .. 2026-10-07"
    includes the whole 7th.
    """
    s = start.strip()
    e = end.strip()
    if len(e) == 10:
        from datetime import date, timedelta
        e = (date.fromisoformat(e) + timedelta(days=1)).isoformat()
    return s, e
