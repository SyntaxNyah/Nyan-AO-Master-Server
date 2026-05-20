"""Moderation: censorship of advertised servers and IP / ASN bans.

Two file-driven mechanisms keep the master server clean:

* ``censors.txt`` — a list of forbidden phrases. Heartbeats whose ``name`` or
  ``description`` contains any phrase are accepted with a normal-looking
  response but **shadow-listed**: the server thinks it is advertised but it
  never appears in ``GET /servers``.

* ``bans.txt`` — IPs, CIDR ranges, or whole ASNs that are denied outright.
  Heartbeats from banned addresses are rejected with ``403``. Admin endpoints
  can add in-memory bans with an optional duration for temporary bans.

ASN bans require a MaxMind GeoLite2-ASN database and the optional
``maxminddb`` package. When neither is configured, ASN entries are still
parsed but silently never match (a warning is logged at startup).

Both files are reloaded automatically on mtime change so an operator can edit
them without restarting the service.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

log = logging.getLogger("master_server.moderation")

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
AsnLookupFn = Callable[[str], Optional[int]]


def _iter_config_lines(path: str) -> Iterator[str]:
    """Yield non-empty, non-comment lines from a config file. Missing file -> empty."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if line:
                    yield line
    except FileNotFoundError:
        return


def _parse_network(value: str) -> Optional[IPNetwork]:
    """Parse a bare IP or CIDR. ``1.2.3.4`` is treated as ``1.2.3.4/32``."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _parse_asn(value: str) -> Optional[int]:
    """Parse ``AS15169`` / ``ASN15169`` / ``as15169`` -> 15169."""
    raw = value.strip().upper()
    for prefix in ("ASN", "AS"):
        if raw.startswith(prefix) and raw[len(prefix):].isdigit():
            return int(raw[len(prefix):])
    return None


def parse_ban_target(value: str) -> Optional[Tuple[str, object]]:
    """Classify a ban target string.

    Returns ``("asn", int)``, ``("net", IPNetwork)``, or ``None``.
    ``AS<n>`` is preferred over IP-style parsing so an ambiguous token never
    becomes a network by accident.
    """
    asn = _parse_asn(value)
    if asn is not None:
        return ("asn", asn)
    net = _parse_network(value)
    if net is not None:
        return ("net", net)
    return None


def _parse_until(value: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp (with optional trailing ``Z``) to epoch seconds."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> Optional[float]:
    """Parse a short duration string like ``30m``, ``24h``, ``7d``, ``90s``."""
    raw = value.strip().lower()
    if not raw:
        return None
    unit = _DURATION_UNITS.get(raw[-1])
    if unit is None:
        # Bare integer -> minutes (matches the admin API).
        try:
            return float(raw) * 60.0
        except ValueError:
            return None
    try:
        amount = float(raw[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount * unit


class Censor:
    """Substring match against a list of forbidden phrases.

    ``censors.txt`` has one phrase per line. Lines starting with ``#`` and
    blank lines are ignored. Matching is case-insensitive and substring-based,
    so ``casino`` matches ``Best Casino Ever``.
    """

    def __init__(self, path: Optional[str]):
        self._path = path
        self._mtime: float = 0.0
        self._words: List[str] = []
        self._loaded = False

    def _maybe_reload(self) -> None:
        if not self._path:
            return
        try:
            mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            if self._loaded and self._words:
                self._words = []
            self._loaded = True
            self._mtime = 0.0
            return
        if self._loaded and mtime == self._mtime:
            return
        self._mtime = mtime
        self._loaded = True
        self._words = [
            line.lower() for line in _iter_config_lines(self._path) if line
        ]

    def is_censored(self, *texts: str) -> bool:
        self._maybe_reload()
        if not self._words:
            return False
        haystack = " ".join(t for t in texts if t).lower()
        if not haystack:
            return False
        return any(word in haystack for word in self._words)


@dataclass
class BanEntry:
    """One ban: either an IP/CIDR network or a whole ASN, never both."""

    network: Optional[IPNetwork]
    asn: Optional[int]
    until: Optional[float]  # epoch seconds; ``None`` = permanent
    source: str             # "file" or "admin"
    reason: str = ""

    @property
    def label(self) -> str:
        return f"AS{self.asn}" if self.asn is not None else str(self.network)

    def expired(self, now: Optional[float] = None) -> bool:
        if self.until is None:
            return False
        return (time.time() if now is None else now) >= self.until

    def matches(
        self,
        addr: Optional[ipaddress._BaseAddress],
        asn: Optional[int],
    ) -> bool:
        if self.network is not None and addr is not None:
            return addr in self.network
        if self.asn is not None and asn is not None:
            return self.asn == asn
        return False


class BanList:
    """File-loaded + in-memory ban list, keyed by IP or CIDR network.

    ``bans.txt`` format (one entry per line)::

        # Permanent IP ban
        1.2.3.4
        # CIDR
        10.0.0.0/8
        # Temporary ban -- absolute deadline
        4.4.4.4 until=2026-12-31T00:00:00Z reason=spam
        # Temporary ban -- relative duration (s/m/h/d, bare number = minutes)
        5.5.5.5 for=24h
        6.6.6.6 for=30m reason=flooding
        # Whole ASN -- needs MaxMind GeoLite2-ASN db configured
        AS15169
        AS13335 for=24h reason="datacenter flood"

    Each entry has its own duration; some can be permanent while others
    expire. Admin-issued bans live in memory only and may also carry a
    duration; they survive file reloads but not a process restart.

    ``asn_lookup`` is an optional ``ip -> asn`` callable. When unset, ASN
    entries are still parsed and listed but never match -- the operator gets
    a warning at startup so the misconfiguration is loud.
    """

    def __init__(
        self,
        path: Optional[str],
        *,
        asn_lookup: Optional[AsnLookupFn] = None,
    ):
        self._path = path
        self._mtime: float = 0.0
        self._loaded = False
        self._file_entries: List[BanEntry] = []
        self._admin_entries: Dict[str, BanEntry] = {}
        self._asn_lookup = asn_lookup

    def set_asn_lookup(self, lookup: Optional[AsnLookupFn]) -> None:
        self._asn_lookup = lookup

    def _maybe_reload(self) -> None:
        if not self._path:
            return
        try:
            mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            if self._loaded and self._file_entries:
                self._file_entries = []
            self._loaded = True
            self._mtime = 0.0
            return
        if self._loaded and mtime == self._mtime:
            return
        self._mtime = mtime
        self._loaded = True
        entries: List[BanEntry] = []
        for line in _iter_config_lines(self._path):
            parts = line.split()
            parsed = parse_ban_target(parts[0])
            if parsed is None:
                log.warning("bans.txt: skipping unparseable target %r", parts[0])
                continue
            kind, value = parsed
            until: Optional[float] = None
            reason = ""
            now = time.time()
            for token in parts[1:]:
                if token.startswith("until="):
                    until = _parse_until(token[len("until="):])
                elif token.startswith("for="):
                    secs = _parse_duration(token[len("for="):])
                    if secs is not None:
                        until = now + secs
                elif token.startswith("reason="):
                    reason = token[len("reason="):].strip('"')
            entries.append(
                BanEntry(
                    network=value if kind == "net" else None,
                    asn=value if kind == "asn" else None,
                    until=until,
                    source="file",
                    reason=reason,
                )
            )
        self._file_entries = entries

    def _active_entries(self, now: Optional[float] = None) -> List[BanEntry]:
        self._maybe_reload()
        # Garbage-collect expired admin entries on access.
        expired_keys = [
            k for k, e in self._admin_entries.items() if e.expired(now)
        ]
        for k in expired_keys:
            del self._admin_entries[k]
        return [e for e in self._file_entries if not e.expired(now)] + list(
            self._admin_entries.values()
        )

    def is_banned(self, ip: str, now: Optional[float] = None) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            addr = None
        entries = self._active_entries(now)
        # Only resolve ASN if we have at least one ASN-typed ban active and
        # an IP we can actually look up. Skips the call entirely on the
        # common path.
        asn: Optional[int] = None
        if (
            addr is not None
            and self._asn_lookup is not None
            and any(e.asn is not None for e in entries)
        ):
            try:
                asn = self._asn_lookup(ip)
            except Exception as exc:  # noqa: BLE001 - never let lookup crash a heartbeat
                log.warning("ASN lookup failed for %s: %s", ip, exc)
        if addr is None and asn is None:
            return False
        for entry in entries:
            if entry.matches(addr, asn):
                return True
        return False

    def _key(self, entry: BanEntry) -> str:
        return entry.label

    def add(
        self,
        target: str,
        *,
        duration_minutes: Optional[float] = None,
        reason: str = "",
        now: Optional[float] = None,
    ) -> BanEntry:
        parsed = parse_ban_target(target)
        if parsed is None:
            raise ValueError(f"invalid IP, CIDR, or ASN: {target!r}")
        kind, value = parsed
        until: Optional[float] = None
        if duration_minutes is not None:
            if duration_minutes <= 0:
                raise ValueError("duration_minutes must be positive")
            base = time.time() if now is None else now
            until = base + duration_minutes * 60.0
        entry = BanEntry(
            network=value if kind == "net" else None,
            asn=value if kind == "asn" else None,
            until=until,
            source="admin",
            reason=reason,
        )
        self._admin_entries[entry.label] = entry
        return entry

    def remove(self, target: str) -> bool:
        """Remove an admin-issued ban. File-loaded bans must be removed from disk."""
        parsed = parse_ban_target(target)
        if parsed is None:
            return False
        kind, value = parsed
        if kind == "asn":
            key = f"AS{value}"
        else:
            key = str(value)
        return self._admin_entries.pop(key, None) is not None

    def list_active(self, now: Optional[float] = None) -> List[dict]:
        out = []
        for entry in self._active_entries(now):
            out.append(
                {
                    "target": entry.label,
                    "kind": "asn" if entry.asn is not None else "net",
                    # Back-compat field for clients of the previous shape.
                    "network": str(entry.network) if entry.network is not None else None,
                    "asn": entry.asn,
                    "until": (
                        datetime.fromtimestamp(entry.until, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if entry.until is not None
                        else None
                    ),
                    "source": entry.source,
                    "reason": entry.reason,
                }
            )
        return out


class MaxMindAsnLookup:
    """``ip -> asn`` lookup backed by a MaxMind GeoLite2-ASN ``.mmdb`` file.

    Maintains a small LRU cache so repeat heartbeats from the same peer do
    not hit the database. Raises :class:`RuntimeError` if the optional
    ``maxminddb`` package isn't installed.
    """

    def __init__(self, path: str, cache_size: int = 4096):
        try:
            import maxminddb  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only without dep
            raise RuntimeError(
                "ASN bans require the optional 'maxminddb' package: "
                "pip install maxminddb"
            ) from exc
        self._reader = maxminddb.open_database(path)
        self._cache: "OrderedDict[str, Optional[int]]" = OrderedDict()
        self._cache_size = cache_size

    def __call__(self, ip: str) -> Optional[int]:
        if ip in self._cache:
            self._cache.move_to_end(ip)
            return self._cache[ip]
        try:
            record = self._reader.get(ip)
        except (ValueError, KeyError, TypeError):
            record = None
        asn = (
            record.get("autonomous_system_number")
            if isinstance(record, dict)
            else None
        )
        self._cache[ip] = asn
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return asn

    def close(self) -> None:
        self._reader.close()
