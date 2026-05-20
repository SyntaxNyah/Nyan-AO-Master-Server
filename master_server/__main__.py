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
    # The stdin console is only useful when the process is attached to a
    # terminal; ``console.py`` no-ops cleanly when stdin is not a TTY.
    app = create_app(config=config, enable_console=True)
    web.run_app(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
