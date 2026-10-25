"""HTTP app: /ingest for Health Auto Export, /mcp for AI assistants."""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .db import Database

log = logging.getLogger("health_mcp.server")

INSTRUCTIONS = """\
Personal Apple Health data for one user, pushed from the iPhone by the
Health Auto Export app (roughly hourly). Timestamps are local wall-clock
time with UTC offset; daily buckets are local calendar days.

How to read aggregates: use `sum` for cumulative metrics (step_count,
active_energy, basal_energy_burned, walking_running_distance, flights_climbed,
dietary_*), `avg` for rate/level metrics (heart_rate, resting_heart_rate,
heart_rate_variability, respiratory_rate, blood_oxygen_saturation, weight_body_mass,
body_fat_percentage). For sleep_analysis each row is one night: `qty` is hours
asleep and `fields` carries inBed/core/deep/rem/awake and sleepStart/sleepEnd.
Start with `list_metrics` to see what exists and the exact metric names.
"""


# ----------------------------------------------------------------------- auth
class BearerAuth:
    """Tiny ASGI middleware: path-prefix -> required token."""

    def __init__(self, app: ASGIApp, rules: dict[str, str], open_paths: set[str]):
        self.app = app
        self.rules = rules
        self.open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in self.open_paths:
            await self.app(scope, receive, send)
            return
        required = next((tok for prefix, tok in self.rules.items() if path.startswith(prefix)), None)
        if required is None:
            await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        presented = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        presented = presented or headers.get("x-api-key", "") or headers.get("x-token", "")
        if not presented or not _const_eq(presented, required):
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="health-mcp"'},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _const_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------------------- ingest
def make_ingest_handler(db: Database):
    async def ingest(request: Request) -> Response:
        raw = await request.body()
        nbytes = len(raw)
        try:
            if request.headers.get("content-encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            result = db.ingest(payload, nbytes)
        except Exception as exc:  # noqa: BLE001 - we want to log whatever the phone sent
            log.warning("ingest failed (%d bytes): %s", nbytes, exc)
            db.log_ingest_error(nbytes, str(exc))
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **result})

    return ingest


# ------------------------------------------------------------------------ mcp
def build_mcp(db: Database, settings: Settings) -> MCPServer:
    tz = ZoneInfo(settings.timezone)
    mcp = MCPServer(
        name="health-mcp",
        title="Apple Health (personal)",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def today() -> date:
        return datetime.now(tz).date()

    @mcp.tool(description="List every metric in the database with units, sample count and first/last timestamp. Call this first to learn the exact metric names.")
    def list_metrics() -> list[dict[str, Any]]:
        return db.list_metrics()

    @mcp.tool(description="Most recent sample of one metric (e.g. 'weight_body_mass', 'resting_heart_rate').")
    def latest(metric: str) -> dict[str, Any] | None:
        return db.latest(metric)

    @mcp.tool(
        description=(
            "Query one metric between two dates (YYYY-MM-DD, end inclusive; ISO timestamps also accepted). "
            "aggregate='daily' returns one row per local day with sum/avg/min/max/samples - use sum for cumulative "
            "metrics like step_count, avg for levels like heart_rate. 'raw' returns individual samples with all fields. "
            "Defaults: last 30 days, daily."
        )
    )
    def query(
        metric: str,
        start: str | None = None,
        end: str | None = None,
        aggregate: Literal["raw", "hourly", "daily", "weekly", "monthly"] = "daily",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        end_d = end or today().isoformat()
        start_d = start or (today() - timedelta(days=30)).isoformat()
        return db.query(metric, start_d, end_d, aggregate, max(1, min(limit, 5000)))

    @mcp.tool(description="All metrics aggregated for one local calendar day (YYYY-MM-DD, default today).")
    def daily_summary(day: str | None = None) -> list[dict[str, Any]]:
        return db.daily_summary(day or today().isoformat())

    @mcp.tool(description="Workouts between two dates (YYYY-MM-DD, end inclusive). Defaults: last 30 days. Includes duration, active kcal, distance km, avg/max HR and all other fields the Watch recorded.")
    def workouts(start: str | None = None, end: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        end_d = end or today().isoformat()
        start_d = start or (today() - timedelta(days=30)).isoformat()
        return db.workouts(start_d, end_d, max(1, min(limit, 1000)))

    @mcp.tool(
        description=(
            "Run a read-only SQL query (SQLite) for anything the other tools don't cover - rolling averages, joins, "
            "regressions. Tables: samples(metric, ts, ts_utc, qty, units, source, fields JSON), "
            "workouts(id, name, start, end, start_utc, end_utc, duration_s, active_kcal, distance_km, avg_hr, max_hr, fields JSON), "
            "ingest_log. Use substr(ts,1,10) for the local day and json_extract(fields,'$.deep') for sub-fields. Max 500 rows."
        )
    )
    def sql(query: str, limit: int = 500) -> dict[str, Any]:
        try:
            rows = db.sql(query, max(1, min(limit, 500)))
        except Exception as exc:  # noqa: BLE001 - surface SQL errors to the model so it can fix the query
            return {"error": f"{type(exc).__name__}: {exc}", "rows": []}
        return {"rows": rows, "row_count": len(rows), "truncated": len(rows) >= max(1, min(limit, 500))}

    @mcp.tool(description="Health of the ingest pipeline: totals, database size and the last uploads from the phone. Use this when data looks stale.")
    def ingest_status() -> dict[str, Any]:
        status = db.ingest_status()
        status["server_time"] = datetime.now(tz).isoformat(timespec="seconds")
        status["timezone"] = settings.timezone
        return status

    return mcp


# ------------------------------------------------------------------------ app
def build_app(settings: Settings) -> Starlette:
    db = Database(settings.db_path)
    mcp = build_mcp(db, settings)

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1 * 1024 * 1024,
        # We sit behind bearer auth on a LAN IP; Host-header checks would only
        # get in the way of clients connecting by IP.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "name": "health-mcp", "version": __version__})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with mcp.session_manager.run():
            log.info("health-mcp %s listening on %s:%d (db: %s)", __version__, settings.host, settings.port, settings.db_path)
            yield
        db.conn.close()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/ingest", make_ingest_handler(db), methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    return BearerAuth(
        app,
        rules={"/ingest": settings.ingest_token, "/mcp": settings.mcp_token},
        open_paths={"/health"},
    )
