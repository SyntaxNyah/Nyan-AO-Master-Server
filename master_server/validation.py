"""Heartbeat input validation.

All heartbeat input is untrusted. Strings are length-bounded, numbers are
range-checked, and missing/invalid ``ip`` or ``port`` are rejected outright.
"""

from __future__ import annotations

from typing import Optional

MAX_IP_LEN = 253
MAX_NAME_LEN = 256
MAX_DESCRIPTION_LEN = 4096
MAX_PLAYERS = 100_000


class ValidationError(Exception):
    """Raised when a heartbeat body fails validation."""


def _is_int(value: object) -> bool:
    # bool is a subclass of int; reject it so True/False can't pose as ports.
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_port(value: object, label: str) -> int:
    if not _is_int(value):
        raise ValidationError(f"{label} must be an integer")
    if not 1 <= value <= 65535:
        raise ValidationError(f"{label} must be between 1 and 65535")
    return value


def _validate_optional_port(value: object, label: str) -> Optional[int]:
    # Absent, null, or 0 all mean "this server has no such port".
    if value is None or value == 0:
        return None
    return _validate_port(value, label)


def _validate_text(value: object, label: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    # Free text is truncated rather than rejected to keep heartbeats forgiving.
    return value[:max_len]


def validate_heartbeat(body: object) -> dict:
    """Validate a heartbeat payload, returning a normalised field dict.

    Raises :class:`ValidationError` for any missing/invalid ``ip`` or ``port``.
    """
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")

    ip = body.get("ip")
    if not isinstance(ip, str) or not ip.strip():
        raise ValidationError("ip is required and must be a non-empty string")
    ip = ip.strip()
    if len(ip) > MAX_IP_LEN:
        raise ValidationError(f"ip must be at most {MAX_IP_LEN} characters")

    port = _validate_port(body.get("port"), "port")

    players = body.get("players", 0)
    if not _is_int(players) or players < 0:
        raise ValidationError("players must be a non-negative integer")
    players = min(players, MAX_PLAYERS)

    return {
        "ip": ip,
        "port": port,
        "name": _validate_text(body.get("name"), "name", MAX_NAME_LEN),
        "description": _validate_text(
            body.get("description"), "description", MAX_DESCRIPTION_LEN
        ),
        "players": players,
        "ws_port": _validate_optional_port(body.get("ws_port"), "ws_port"),
        "wss_port": _validate_optional_port(body.get("wss_port"), "wss_port"),
    }
