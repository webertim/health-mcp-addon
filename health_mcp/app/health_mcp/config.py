"""Runtime configuration.

Inside Home Assistant the Supervisor writes the add-on options to
/data/options.json and gives us /data as persistent storage. Outside HA
(local development / tests) everything can be overridden with env vars.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("health_mcp.config")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    ingest_token: str
    mcp_token: str
    timezone: str
    log_level: str
    host: str
    port: int

    @property
    def generated_tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"


def _read_options(data_dir: Path) -> dict:
    options_file = Path(os.environ.get("HEALTH_MCP_OPTIONS", data_dir / "options.json"))
    if options_file.exists():
        try:
            return json.loads(options_file.read_text())
        except json.JSONDecodeError as exc:
            log.error("Could not parse %s: %s", options_file, exc)
    return {}


def _token(name: str, options: dict, data_dir: Path) -> str:
    """Return the configured token, or a generated one persisted in /data.

    Generated tokens are printed to the add-on log once so the user can copy
    them; setting the option in the add-on configuration always wins.
    """
    configured = (os.environ.get(f"HEALTH_MCP_{name.upper()}") or options.get(name) or "").strip()
    if configured:
        return configured

    store = data_dir / "tokens.json"
    tokens: dict[str, str] = {}
    if store.exists():
        try:
            tokens = json.loads(store.read_text())
        except json.JSONDecodeError:
            tokens = {}
    if not tokens.get(name):
        tokens[name] = secrets.token_urlsafe(32)
        store.write_text(json.dumps(tokens, indent=2))
        try:
            store.chmod(0o600)
        except OSError:
            pass
        log.warning("No %s configured - generated one and stored it in %s", name, store)
    return tokens[name]


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("HEALTH_MCP_DATA", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    options = _read_options(data_dir)

    log_level = (os.environ.get("HEALTH_MCP_LOG_LEVEL") or options.get("log_level") or "info").upper()
    logging.getLogger().setLevel(log_level)

    return Settings(
        data_dir=data_dir,
        db_path=Path(os.environ.get("HEALTH_MCP_DB", data_dir / "health.db")),
        ingest_token=_token("ingest_token", options, data_dir),
        mcp_token=_token("mcp_token", options, data_dir),
        timezone=os.environ.get("HEALTH_MCP_TZ") or options.get("timezone") or "UTC",
        log_level=log_level,
        host=os.environ.get("HEALTH_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEALTH_MCP_PORT", "8099")),
    )
