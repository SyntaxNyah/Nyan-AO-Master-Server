"""Storage layer for registered game servers.

The :class:`Storage` abstract base class defines the contract every backend
must satisfy. v1 ships only :class:`InMemoryStorage`; a SQLite-backed
implementation can be dropped in later without touching the web layer.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Server:
    """A single registered game server."""

    ip: str
    port: int
    name: str = ""
    description: str = ""
    players: int = 0
    ws_port: Optional[int] = None
    wss_port: Optional[int] = None
    hbcounter: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        """Unique identity of a server: its ip:port pair."""
        return f"{self.ip}:{self.port}"

    def public_dict(self) -> dict:
        """The shape exposed by ``GET /servers``.

        ``ws_port`` / ``wss_port`` are omitted entirely when unset so clients
        never see ``0``/``null`` noise.
        """
        data = {
            "ip": self.ip,
            "port": self.port,
            "name": self.name,
            "description": self.description,
            "players": self.players,
            "hbcounter": self.hbcounter,
        }
        if self.ws_port is not None:
            data["ws_port"] = self.ws_port
        if self.wss_port is not None:
            data["wss_port"] = self.wss_port
        return data


def next_hbcounter(current: int, cap: int, drop: int) -> int:
    """Increment a heartbeat counter, applying rollover.

    The counter rises by one each heartbeat. Once it reaches ``cap`` it is
    dropped to ``cap - drop`` and keeps climbing, matching what AO monitoring
    tooling expects to observe.
    """
    nxt = current + 1
    if nxt >= cap:
        return cap - drop
    return nxt


class Storage(abc.ABC):
    """Abstract storage backend for registered servers."""

    @abc.abstractmethod
    async def upsert(
        self,
        fields: dict,
        *,
        hbcounter_cap: int,
        hbcounter_rollover_drop: int,
        now: Optional[float] = None,
    ) -> Server:
        """Register or refresh a server from validated heartbeat ``fields``.

        Updates ``last_seen`` to ``now`` and increments ``hbcounter`` (with
        rollover). Returns the stored :class:`Server`.
        """

    @abc.abstractmethod
    async def active_servers(
        self, expiry_seconds: float, now: Optional[float] = None
    ) -> List[Server]:
        """Return servers seen within ``expiry_seconds``, sorted by key."""

    @abc.abstractmethod
    async def purge_stale(
        self, expiry_seconds: float, now: Optional[float] = None
    ) -> int:
        """Drop servers not seen within ``expiry_seconds``. Returns count removed."""


class InMemoryStorage(Storage):
    """A process-local, dict-backed :class:`Storage` implementation."""

    def __init__(self) -> None:
        self._servers: Dict[str, Server] = {}
        self._lock = asyncio.Lock()

    async def upsert(
        self,
        fields: dict,
        *,
        hbcounter_cap: int,
        hbcounter_rollover_drop: int,
        now: Optional[float] = None,
    ) -> Server:
        now = time.time() if now is None else now
        key = f"{fields['ip']}:{fields['port']}"
        async with self._lock:
            existing = self._servers.get(key)
            hbcounter = next_hbcounter(
                existing.hbcounter if existing else 0,
                hbcounter_cap,
                hbcounter_rollover_drop,
            )
            server = Server(
                ip=fields["ip"],
                port=fields["port"],
                name=fields.get("name", ""),
                description=fields.get("description", ""),
                players=fields.get("players", 0),
                ws_port=fields.get("ws_port"),
                wss_port=fields.get("wss_port"),
                hbcounter=hbcounter,
                last_seen=now,
            )
            self._servers[key] = server
            return server

    async def active_servers(
        self, expiry_seconds: float, now: Optional[float] = None
    ) -> List[Server]:
        now = time.time() if now is None else now
        cutoff = now - expiry_seconds
        async with self._lock:
            active = [s for s in self._servers.values() if s.last_seen >= cutoff]
        return sorted(active, key=lambda s: s.key)

    async def purge_stale(
        self, expiry_seconds: float, now: Optional[float] = None
    ) -> int:
        now = time.time() if now is None else now
        cutoff = now - expiry_seconds
        async with self._lock:
            stale = [k for k, s in self._servers.items() if s.last_seen < cutoff]
            for key in stale:
                del self._servers[key]
        return len(stale)
