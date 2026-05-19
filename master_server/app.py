"""aiohttp application: routes, background expiry task, app factory."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from master_server.config import Config
from master_server.storage import InMemoryStorage, Storage
from master_server.validation import ValidationError, validate_heartbeat

log = logging.getLogger("master_server")


async def handle_root(request: web.Request) -> web.Response:
    """Tiny service banner / health check."""
    return web.json_response(
        {
            "service": "nyan-ao-master-server",
            "endpoints": {
                "servers": "GET /servers",
                "heartbeat": "POST /heartbeat",
            },
        }
    )


async def handle_servers(request: web.Request) -> web.Response:
    """``GET /servers`` -> JSON array of currently-registered servers."""
    config: Config = request.app["config"]
    storage: Storage = request.app["storage"]
    servers = await storage.active_servers(config.heartbeat_expiry_seconds)
    return web.json_response([s.public_dict() for s in servers])


async def handle_heartbeat(request: web.Request) -> web.Response:
    """``POST /heartbeat`` or ``POST /servers`` -> register/refresh a server, return its record."""
    config: Config = request.app["config"]
    storage: Storage = request.app["storage"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be valid JSON"}, status=400)

    # Fall back to the connecting IP when the client omits the "ip" field
    # (e.g. akashi only sends "ip" when serverDomainName is configured).
    if isinstance(body, dict) and not body.get("ip"):
        body = {**body, "ip": request.remote}

    try:
        fields = validate_heartbeat(body)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    server = await storage.upsert(
        fields,
        hbcounter_cap=config.hbcounter_cap,
        hbcounter_rollover_drop=config.hbcounter_rollover_drop,
    )
    return web.json_response(server.public_dict())


async def _purge_loop(app: web.Application) -> None:
    """Background task: periodically drop stale servers."""
    config: Config = app["config"]
    storage: Storage = app["storage"]
    try:
        while True:
            await asyncio.sleep(config.purge_interval_seconds)
            removed = await storage.purge_stale(config.heartbeat_expiry_seconds)
            if removed:
                log.info("purged %d stale server(s)", removed)
    except asyncio.CancelledError:
        pass


async def _start_background_tasks(app: web.Application) -> None:
    app["purge_task"] = asyncio.create_task(_purge_loop(app))


async def _cleanup_background_tasks(app: web.Application) -> None:
    task = app.get("purge_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_app(
    config: Optional[Config] = None, storage: Optional[Storage] = None
) -> web.Application:
    """Build the aiohttp application.

    ``config``/``storage`` are injectable to keep the app testable; defaults
    come from the environment and an in-memory store.
    """
    app = web.Application()
    app["config"] = config if config is not None else Config.from_env()
    app["storage"] = storage if storage is not None else InMemoryStorage()

    app.router.add_get("/", handle_root)
    app.router.add_get("/servers", handle_servers)
    app.router.add_post("/servers", handle_heartbeat)  # akashi advertises here
    app.router.add_post("/heartbeat", handle_heartbeat)

    app.on_startup.append(_start_background_tasks)
    app.on_cleanup.append(_cleanup_background_tasks)
    return app
