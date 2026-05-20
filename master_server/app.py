"""aiohttp application: routes, background expiry task, app factory."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from master_server.config import Config
from master_server.console import start_console
from master_server.moderation import BanList, Censor, MaxMindAsnLookup
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
    return web.json_response(
        [s.public_dict() for s in servers],
        headers={"Cache-Control": "no-store"},
    )


def _client_ip(request: web.Request) -> Optional[str]:
    """Best-effort real client IP. Honours proxy headers, falls back to peer."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    real_ip = request.headers.get("X-Real-IP")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if real_ip:
        return real_ip
    return request.remote


async def handle_heartbeat(request: web.Request) -> web.Response:
    """``POST /heartbeat`` or ``POST /servers`` -> register/refresh a server."""
    config: Config = request.app["config"]
    storage: Storage = request.app["storage"]
    bans: BanList = request.app["bans"]
    censor: Censor = request.app["censor"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be valid JSON"}, status=400)

    # Fall back to the connecting IP when the client omits the "ip" field
    # (e.g. akashi only sends "ip" when serverDomainName is configured).
    if isinstance(body, dict) and not body.get("ip"):
        body = {**body, "ip": _client_ip(request)}

    try:
        fields = validate_heartbeat(body)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    # IP bans short-circuit: never accept the heartbeat. We check both the
    # advertised ip and the real connecting peer so a banned operator can't
    # work around it by lying about ``ip``.
    peer_ip = _client_ip(request)
    if bans.is_banned(fields["ip"]) or (peer_ip and bans.is_banned(peer_ip)):
        return web.json_response({"error": "banned"}, status=403)

    # Censorship: store the record but shadow-list it. The advertiser sees a
    # normal heartbeat response (and the counter keeps ticking) but the server
    # never appears in ``GET /servers``.
    shadowed = censor.is_censored(fields.get("name", ""), fields.get("description", ""))

    server = await storage.upsert(
        fields,
        hbcounter_cap=config.hbcounter_cap,
        hbcounter_rollover_drop=config.hbcounter_rollover_drop,
        shadowed=shadowed,
    )
    return web.json_response(server.public_dict())


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #


def _admin_authorised(request: web.Request) -> bool:
    token: str = request.app["config"].admin_token
    if not token:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header[len("Bearer "):].strip() == token


def _admin_guard(request: web.Request) -> Optional[web.Response]:
    if not request.app["config"].admin_token:
        return web.json_response(
            {"error": "admin endpoints disabled (set MS_ADMIN_TOKEN)"}, status=503
        )
    if not _admin_authorised(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    return None


async def handle_admin_kick(request: web.Request) -> web.Response:
    """``POST /admin/kick`` -- drop a registered server. Body: ``{"ip", "port"?}``."""
    guard = _admin_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    ip = body.get("ip")
    if not isinstance(ip, str) or not ip.strip():
        return web.json_response({"error": "ip is required"}, status=400)
    port = body.get("port")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool)):
        return web.json_response({"error": "port must be an integer"}, status=400)
    storage: Storage = request.app["storage"]
    removed = await storage.kick(ip.strip(), port)
    return web.json_response({"removed": removed})


def _extract_ban_target(body: dict) -> Optional[str]:
    """Pull an ASN/IP/CIDR target out of an admin request body.

    Accepts ``{"asn": 15169}``, ``{"ip": "AS15169"}``, or ``{"ip": "1.2.3.4"}``
    so the same handler covers all three forms.
    """
    asn = body.get("asn")
    if asn is not None:
        if isinstance(asn, bool) or not isinstance(asn, int) or asn <= 0:
            return None
        return f"AS{asn}"
    ip = body.get("ip") or body.get("target")
    if isinstance(ip, str) and ip.strip():
        return ip.strip()
    return None


async def handle_admin_ban(request: web.Request) -> web.Response:
    """``POST /admin/ban`` -- add a ban.

    Body: ``{"ip"|"target": "1.2.3.4" | "AS15169", "duration_minutes"?, "reason"?}``
    or ``{"asn": 15169, "duration_minutes"?, "reason"?}``.

    Omit ``duration_minutes`` for a permanent ban. Any server matching the
    new ban is kicked from the live listing immediately.
    """
    guard = _admin_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    target = _extract_ban_target(body)
    if target is None:
        return web.json_response(
            {"error": "ip, target, or asn is required"}, status=400
        )
    duration = body.get("duration_minutes")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return web.json_response(
                {"error": "duration_minutes must be a number"}, status=400
            )
        if duration <= 0:
            return web.json_response(
                {"error": "duration_minutes must be positive"}, status=400
            )
    reason = body.get("reason") or ""
    if not isinstance(reason, str):
        return web.json_response({"error": "reason must be a string"}, status=400)

    bans: BanList = request.app["bans"]
    storage: Storage = request.app["storage"]
    try:
        entry = bans.add(
            target,
            duration_minutes=float(duration) if duration is not None else None,
            reason=reason[:256],
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # Drop anyone matching the new ban from the live listing right away.
    # is_banned resolves ASN if needed, so ASN bans kick existing servers too.
    kicked = await storage.kick_matching(lambda s: bans.is_banned(s.ip))
    log.info("banned %s (duration=%s); kicked %d server(s)", entry.label, duration, kicked)
    return web.json_response(
        {"banned": entry.label, "kicked": kicked, "bans": bans.list_active()}
    )


async def handle_admin_unban(request: web.Request) -> web.Response:
    """``DELETE /admin/ban`` -- remove an admin-issued ban.

    Body: ``{"ip"|"target": "1.2.3.4" | "AS15169"}`` or ``{"asn": 15169}``.
    """
    guard = _admin_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    target = _extract_ban_target(body)
    if target is None:
        return web.json_response(
            {"error": "ip, target, or asn is required"}, status=400
        )
    bans: BanList = request.app["bans"]
    removed = bans.remove(target)
    return web.json_response({"removed": removed})


async def handle_admin_bans(request: web.Request) -> web.Response:
    """``GET /admin/bans`` -- list currently active bans (file + admin)."""
    guard = _admin_guard(request)
    if guard is not None:
        return guard
    bans: BanList = request.app["bans"]
    return web.json_response({"bans": bans.list_active()})


# --------------------------------------------------------------------------- #
# Background tasks & app factory
# --------------------------------------------------------------------------- #


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
    # Console runs only when stdin is interactive and only in the live app
    # (tests opt out with ``enable_console=False``).
    if app.get("enable_console", False):
        app["console_task"] = start_console(app)


async def _cleanup_background_tasks(app: web.Application) -> None:
    for key in ("purge_task", "console_task"):
        task = app.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def create_app(
    config: Optional[Config] = None,
    storage: Optional[Storage] = None,
    *,
    enable_console: bool = False,
) -> web.Application:
    """Build the aiohttp application.

    ``config``/``storage`` are injectable to keep the app testable; defaults
    come from the environment and an in-memory store. ``enable_console=True``
    starts the stdin-driven admin console alongside the HTTP server.
    """
    app = web.Application()
    app["config"] = config if config is not None else Config.from_env()
    app["storage"] = storage if storage is not None else InMemoryStorage()
    app["censor"] = Censor(app["config"].censors_path or None)
    asn_lookup = None
    if app["config"].asn_db_path:
        try:
            asn_lookup = MaxMindAsnLookup(app["config"].asn_db_path)
            log.info("ASN bans enabled (db=%s)", app["config"].asn_db_path)
        except Exception as exc:  # noqa: BLE001 - log and continue without ASN
            log.warning(
                "ASN bans disabled: could not open %s (%s). "
                "AS<n> entries will be parsed but never match.",
                app["config"].asn_db_path,
                exc,
            )
    app["bans"] = BanList(app["config"].bans_path or None, asn_lookup=asn_lookup)
    app["enable_console"] = enable_console

    app.router.add_get("/", handle_root)
    app.router.add_get("/servers", handle_servers)
    app.router.add_post("/servers", handle_heartbeat)  # akashi advertises here
    app.router.add_post("/heartbeat", handle_heartbeat)

    app.router.add_post("/admin/kick", handle_admin_kick)
    app.router.add_post("/admin/ban", handle_admin_ban)
    app.router.add_delete("/admin/ban", handle_admin_unban)
    app.router.add_get("/admin/bans", handle_admin_bans)

    app.on_startup.append(_start_background_tasks)
    app.on_cleanup.append(_cleanup_background_tasks)
    return app
