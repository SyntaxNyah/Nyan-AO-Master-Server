"""Entry point: ``python -m master_server``."""

from __future__ import annotations

import logging

from aiohttp import web

from master_server.app import create_app
from master_server.config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    app = create_app(config=config)
    web.run_app(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
