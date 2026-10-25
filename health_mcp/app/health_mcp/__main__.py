from __future__ import annotations

import logging

import uvicorn

from . import __version__
from .config import load_settings
from .server import build_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    log = logging.getLogger("health_mcp")
    log.info("Health MCP add-on %s starting", __version__)
    log.info("Ingest endpoint : POST http://<host>:%d/ingest   (Authorization: Bearer <ingest_token>)", settings.port)
    log.info("MCP endpoint    : http://<host>:%d/mcp           (Authorization: Bearer <mcp_token>)", settings.port)
    if settings.generated_tokens_file.exists():
        log.warning(
            "Tokens were auto-generated. Current values -> ingest_token: %s   mcp_token: %s",
            settings.ingest_token, settings.mcp_token,
        )
        log.warning("Set them explicitly in the add-on Configuration tab to stop this message.")

    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG",
    )


if __name__ == "__main__":
    main()
