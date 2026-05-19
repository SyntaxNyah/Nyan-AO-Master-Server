"""Tests for the storage layer: registration, refresh, hbcounter, expiry."""

import pytest

from master_server.storage import InMemoryStorage, next_hbcounter

CAP = 50000
DROP = 1000


def fields(ip="1.2.3.4", port=27016, **extra):
    return {"ip": ip, "port": port, **extra}


async def test_registration_creates_server():
    store = InMemoryStorage()
    server = await store.upsert(
        fields(name="Test Court", players=3),
        hbcounter_cap=CAP,
        hbcounter_rollover_drop=DROP,
    )
    assert server.key == "1.2.3.4:27016"
    assert server.name == "Test Court"
    assert server.players == 3
    # First heartbeat takes the counter from 0 to 1.
    assert server.hbcounter == 1


async def test_heartbeat_refresh_updates_in_place():
    store = InMemoryStorage()
    await store.upsert(
        fields(players=1), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=0.0
    )
    refreshed = await store.upsert(
        fields(players=9, name="Renamed"),
        hbcounter_cap=CAP,
        hbcounter_rollover_drop=DROP,
        now=120.0,
    )
    active = await store.active_servers(expiry_seconds=1800, now=120.0)
    # Same ip:port -> still a single record, fields updated.
    assert len(active) == 1
    assert refreshed.players == 9
    assert refreshed.name == "Renamed"
    # 2 minutes of real uptime since registration -> 1 + 2.
    assert refreshed.hbcounter == 3


async def test_hbcounter_advances_one_per_minute():
    store = InMemoryStorage()
    for minute, expected in enumerate([1, 2, 3, 4, 5]):
        server = await store.upsert(
            fields(),
            hbcounter_cap=CAP,
            hbcounter_rollover_drop=DROP,
            now=minute * 60.0,
        )
        assert server.hbcounter == expected


async def test_hbcounter_advances_by_elapsed_minutes():
    # A heartbeat 5 minutes after registration advances the counter by 5.
    store = InMemoryStorage()
    await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=0.0
    )  # hbcounter -> 1
    server = await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=5 * 60.0
    )
    assert server.hbcounter == 6  # 1 + 5 minutes
    # Leftover seconds carry: a beat 90s later only adds one whole minute.
    server = await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=5 * 60.0 + 90.0
    )
    assert server.hbcounter == 7


async def test_rapid_heartbeats_do_not_inflate_hbcounter():
    # Flooding the master with sub-minute heartbeats must not advance the
    # counter -- it tracks real elapsed time, not ping volume.
    store = InMemoryStorage()
    await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=0.0
    )
    for t in (1.0, 2.0, 30.0, 59.0):
        server = await store.upsert(
            fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=t
        )
        assert server.hbcounter == 1
    # Leftover seconds still accrue: a beat at 70s crosses one whole minute.
    server = await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=70.0
    )
    assert server.hbcounter == 2


async def test_hbcounter_rollover():
    # Pure rollover arithmetic (whole minutes elapsed).
    assert next_hbcounter(49998, 1, CAP, DROP) == 49999
    assert next_hbcounter(49999, 1, CAP, DROP) == CAP - DROP  # 49000
    assert next_hbcounter(CAP - DROP, 1, CAP, DROP) == CAP - DROP + 1
    # A multi-minute gap that crosses the cap still rolls over.
    assert next_hbcounter(49998, 5, CAP, DROP) == CAP - DROP
    # Sub-minute beats (0 whole minutes) leave the counter untouched.
    assert next_hbcounter(1234, 0, CAP, DROP) == 1234


async def test_hbcounter_rollover_through_storage():
    # Use a tiny cap so rollover is reachable in a handful of heartbeats,
    # one heartbeat per real minute.
    store = InMemoryStorage()
    counters = []
    for minute in range(8):
        server = await store.upsert(
            fields(), hbcounter_cap=5, hbcounter_rollover_drop=2, now=minute * 60.0
        )
        counters.append(server.hbcounter)
    # cap=5, drop=2 -> rolls to 3 when it would hit 5.
    assert counters == [1, 2, 3, 4, 3, 4, 3, 4]


async def test_stale_servers_expire_from_listing():
    store = InMemoryStorage()
    await store.upsert(
        fields(), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=1000.0
    )
    expiry = 1800  # 30 minutes

    # Still fresh 10 minutes later.
    active = await store.active_servers(expiry, now=1000.0 + 600)
    assert len(active) == 1

    # Gone 31 minutes later.
    active = await store.active_servers(expiry, now=1000.0 + 1860)
    assert active == []


async def test_purge_stale_removes_records():
    store = InMemoryStorage()
    await store.upsert(
        fields(ip="1.1.1.1"), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=0.0
    )
    await store.upsert(
        fields(ip="2.2.2.2"), hbcounter_cap=CAP, hbcounter_rollover_drop=DROP, now=5000.0
    )
    removed = await store.purge_stale(expiry_seconds=1800, now=5000.0)
    assert removed == 1
    remaining = await store.active_servers(expiry_seconds=1800, now=5000.0)
    assert [s.ip for s in remaining] == ["2.2.2.2"]
