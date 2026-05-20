"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class Config:
    """Master server configuration.

    Every value can be overridden through an environment variable so the
    service can be tuned without code changes.
    """

    host: str = "0.0.0.0"
    port: int = 8000
    heartbeat_expiry_minutes: float = 60.0
    hbcounter_cap: int = 10080       # 1 hb/min × 60 min × 24 hr × 7 days
    hbcounter_rollover_drop: int = 1080  # rolls back to 9000
    purge_interval_seconds: float = 60.0
    # Moderation: when set, these files are watched and hot-reloaded on mtime
    # change. Empty string disables the corresponding feature.
    censors_path: str = "censors.txt"
    bans_path: str = "bans.txt"
    # Shared secret for /admin/* endpoints. Unset disables admin routes.
    admin_token: str = ""
    # MaxMind GeoLite2-ASN ``.mmdb`` path. When set (and the optional
    # ``maxminddb`` package is installed), ``AS<n>`` entries in bans.txt and
    # the admin API are honoured. Unset = ASN bans are parsed but never match.
    asn_db_path: str = ""

    @property
    def heartbeat_expiry_seconds(self) -> float:
        return self.heartbeat_expiry_minutes * 60.0

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        defaults = cls()
        cfg = cls(
            host=env.get("MS_HOST", defaults.host),
            port=int(env.get("MS_PORT", defaults.port)),
            heartbeat_expiry_minutes=float(
                env.get("MS_HEARTBEAT_EXPIRY_MINUTES", defaults.heartbeat_expiry_minutes)
            ),
            hbcounter_cap=int(env.get("MS_HBCOUNTER_CAP", defaults.hbcounter_cap)),
            hbcounter_rollover_drop=int(
                env.get("MS_HBCOUNTER_ROLLOVER_DROP", defaults.hbcounter_rollover_drop)
            ),
            purge_interval_seconds=float(
                env.get("MS_PURGE_INTERVAL_SECONDS", defaults.purge_interval_seconds)
            ),
            censors_path=env.get("MS_CENSORS_PATH", defaults.censors_path),
            bans_path=env.get("MS_BANS_PATH", defaults.bans_path),
            admin_token=env.get("MS_ADMIN_TOKEN", defaults.admin_token),
            asn_db_path=env.get("MS_ASN_DB_PATH", defaults.asn_db_path),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("MS_PORT must be between 1 and 65535")
        if self.heartbeat_expiry_minutes <= 0:
            raise ValueError("MS_HEARTBEAT_EXPIRY_MINUTES must be positive")
        if self.hbcounter_cap <= 0:
            raise ValueError("MS_HBCOUNTER_CAP must be positive")
        # The rollover target (cap - drop) must stay non-negative and below the
        # cap, otherwise the counter could never roll over.
        if not 0 < self.hbcounter_rollover_drop <= self.hbcounter_cap:
            raise ValueError(
                "MS_HBCOUNTER_ROLLOVER_DROP must be > 0 and <= MS_HBCOUNTER_CAP"
            )
        if self.purge_interval_seconds <= 0:
            raise ValueError("MS_PURGE_INTERVAL_SECONDS must be positive")
