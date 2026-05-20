"""Interactive stdin console for moderating a running master server.

When the master is started with ``--console`` (or ``python -m master_server``
in an interactive terminal), the operator can type commands directly at the
process to ban/unban/kick servers without making HTTP requests::

    > ban 1.2.3.4 for=30m reason=flooding
    banned 1.2.3.4/32 until 2026-05-20T08:31:00Z; kicked 1 server(s)
    > unban 1.2.3.4
    unbanned 1.2.3.4
    > kick play.example.com 27016
    kicked 1 server(s) matching play.example.com:27016
    > help
    ...

The console is started as an asyncio task and reads stdin in a worker thread
so it never blocks the event loop. If stdin is not a TTY (e.g. running under
systemd without a TTY), the console exits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, List, Optional

from master_server.moderation import BanList, _parse_duration

if TYPE_CHECKING:
    from aiohttp import web

log = logging.getLogger("master_server.console")

_HELP = """\
Commands:
  ban <ip|cidr> [for=<dur>] [reason=<text>]   Ban an IP or CIDR. Omit "for" for permanent.
                                              <dur> = 30s | 15m | 24h | 7d (bare number = minutes).
  unban <ip|cidr>                             Remove an admin-issued ban.
  kick <ip> [port]                            Drop a registered server (or all from <ip>).
  bans                                        List active bans.
  servers                                     List currently registered servers.
  reload                                      Force-reload censors.txt and bans.txt.
  help                                        Show this help.
  quit                                        Stop the master server.
"""


def _parse_tokens(rest: List[str]) -> dict:
    """Pull ``key=value`` tokens out of a command tail."""
    out: dict = {}
    for token in rest:
        if "=" in token:
            k, v = token.split("=", 1)
            out[k.strip().lower()] = v.strip().strip('"')
    return out


async def _handle_command(app: "web.Application", line: str) -> str:
    parts = line.strip().split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    args = parts[1:]
    bans: BanList = app["bans"]
    storage = app["storage"]
    censor = app["censor"]

    if cmd in ("help", "?"):
        return _HELP.rstrip()

    if cmd == "ban":
        if not args:
            return "usage: ban <ip|cidr> [for=<dur>] [reason=<text>]"
        target = args[0]
        kv = _parse_tokens(args[1:])
        duration_minutes: Optional[float] = None
        if "for" in kv:
            secs = _parse_duration(kv["for"])
            if secs is None or secs <= 0:
                return f"invalid duration: {kv['for']!r}"
            duration_minutes = secs / 60.0
        try:
            entry = bans.add(
                target,
                duration_minutes=duration_minutes,
                reason=kv.get("reason", "")[:256],
            )
        except ValueError as exc:
            return f"error: {exc}"
        kicked = await storage.kick_matching(lambda s: bans.is_banned(s.ip))
        if entry.until is None:
            tail = "permanently"
        else:
            from datetime import datetime, timezone

            iso = (
                datetime.fromtimestamp(entry.until, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            tail = f"until {iso}"
        return f"banned {entry.network} {tail}; kicked {kicked} server(s)"

    if cmd == "unban":
        if not args:
            return "usage: unban <ip|cidr>"
        removed = bans.remove(args[0])
        if removed:
            return f"unbanned {args[0]}"
        return f"{args[0]} is not an admin-issued ban (file bans must be removed from bans.txt)"

    if cmd == "kick":
        if not args:
            return "usage: kick <ip> [port]"
        ip = args[0]
        port: Optional[int] = None
        if len(args) >= 2:
            try:
                port = int(args[1])
            except ValueError:
                return f"invalid port: {args[1]!r}"
        removed = await storage.kick(ip, port)
        target = f"{ip}:{port}" if port is not None else ip
        return f"kicked {removed} server(s) matching {target}"

    if cmd == "bans":
        active = bans.list_active()
        if not active:
            return "(no active bans)"
        lines = []
        for e in active:
            tail = f"until {e['until']}" if e["until"] else "permanent"
            extras = f" reason={e['reason']!r}" if e["reason"] else ""
            lines.append(f"  {e['network']:<20} [{e['source']}] {tail}{extras}")
        return "\n".join(lines)

    if cmd == "servers":
        config = app["config"]
        active = await storage.active_servers(config.heartbeat_expiry_seconds)
        if not active:
            return "(no registered servers)"
        lines = []
        for s in active:
            lines.append(
                f"  {s.ip}:{s.port:<6} players={s.players} hb={s.hbcounter} {s.name!r}"
            )
        return "\n".join(lines)

    if cmd == "reload":
        # Force a reload by zeroing the cached mtimes.
        censor._mtime = 0.0  # noqa: SLF001 - internal forced reload
        censor._loaded = False
        bans._mtime = 0.0
        bans._loaded = False
        censor._maybe_reload()
        bans._maybe_reload()
        return "reloaded censors.txt and bans.txt"

    if cmd in ("quit", "exit"):
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, lambda: loop.stop())
        return "stopping..."

    return f"unknown command: {cmd!r} (try 'help')"


async def _console_loop(app: "web.Application") -> None:
    if not sys.stdin or not sys.stdin.isatty():
        log.info("console: stdin is not a TTY; console disabled")
        return
    loop = asyncio.get_running_loop()
    sys.stdout.write("master-server console ready -- type 'help' for commands\n")
    sys.stdout.flush()
    try:
        while True:
            sys.stdout.write("> ")
            sys.stdout.flush()
            # Read a line in a worker thread so we don't block the event loop.
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF (Ctrl-D)
                sys.stdout.write("\n")
                return
            try:
                reply = await _handle_command(app, line)
            except Exception as exc:  # noqa: BLE001 - surface to operator
                log.exception("console command failed")
                reply = f"error: {exc}"
            if reply:
                sys.stdout.write(reply + "\n")
                sys.stdout.flush()
    except asyncio.CancelledError:
        pass


def start_console(app: "web.Application") -> asyncio.Task:
    """Start the stdin console as a background task."""
    return asyncio.create_task(_console_loop(app))
