"""Tests for the HTTP endpoints."""

import pytest

from master_server.app import create_app
from master_server.config import Config
from master_server.storage import InMemoryStorage


@pytest.fixture
def config():
    return Config(
        host="127.0.0.1",
        port=8000,
        heartbeat_expiry_minutes=30.0,
        hbcounter_cap=50000,
        hbcounter_rollover_drop=1000,
        purge_interval_seconds=3600.0,
    )


@pytest.fixture
async def client(aiohttp_client, config):
    app = create_app(config=config, storage=InMemoryStorage())
    return await aiohttp_client(app)


async def test_servers_starts_empty(client):
    resp = await client.get("/servers")
    assert resp.status == 200
    assert await resp.json() == []


async def test_heartbeat_registers_and_appears_in_listing(client):
    resp = await client.post(
        "/heartbeat",
        json={
            "ip": "play.example.com",
            "port": 27016,
            "name": "Example Court",
            "description": "A test server",
            "players": 4,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ip"] == "play.example.com"
    assert body["hbcounter"] == 1

    resp = await client.get("/servers")
    servers = await resp.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "Example Court"
    assert servers[0]["players"] == 4


async def test_heartbeat_refresh_does_not_inflate_hbcounter(client):
    # Rapid refreshes within the same minute must not advance the counter --
    # hbcounter tracks real elapsed time, not the number of pings.
    payload = {"ip": "1.2.3.4", "port": 27016}
    first = await (await client.post("/heartbeat", json=payload)).json()
    second = await (await client.post("/heartbeat", json=payload)).json()
    assert first["hbcounter"] == 1
    assert second["hbcounter"] == 1

    servers = await (await client.get("/servers")).json()
    assert len(servers) == 1


async def test_optional_ws_ports_omitted_when_absent(client):
    body = await (
        await client.post("/heartbeat", json={"ip": "1.2.3.4", "port": 27016})
    ).json()
    assert "ws_port" not in body
    assert "wss_port" not in body


async def test_optional_ws_ports_present_when_supplied(client):
    body = await (
        await client.post(
            "/heartbeat",
            json={"ip": "1.2.3.4", "port": 27016, "ws_port": 2095, "wss_port": 2096},
        )
    ).json()
    assert body["ws_port"] == 2095
    assert body["wss_port"] == 2096


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"port": 27016},
        {"ip": "", "port": 27016},
        {"ip": "  ", "port": 27016},
        {"ip": "1.2.3.4"},
        {"ip": "1.2.3.4", "port": 0},
        {"ip": "1.2.3.4", "port": 99999},
        {"ip": "1.2.3.4", "port": "27016"},
        {"ip": "1.2.3.4", "port": True},
        {"ip": 1234, "port": 27016},
    ],
)
async def test_heartbeat_rejects_invalid_input(client, payload):
    resp = await client.post("/heartbeat", json=payload)
    assert resp.status == 400
    assert "error" in await resp.json()


async def test_heartbeat_rejects_non_json_body(client):
    resp = await client.post("/heartbeat", data="not json")
    assert resp.status == 400


async def test_heartbeat_truncates_long_free_text(client):
    body = await (
        await client.post(
            "/heartbeat",
            json={"ip": "1.2.3.4", "port": 27016, "name": "x" * 5000},
        )
    ).json()
    assert len(body["name"]) == 256


# --------------------------------------------------------------------------- #
# Moderation: censors, bans, admin endpoints
# --------------------------------------------------------------------------- #


@pytest.fixture
def censors_file(tmp_path):
    p = tmp_path / "censors.txt"
    p.write_text("casino\nfree gems\n")
    return p


@pytest.fixture
def admin_config(tmp_path, censors_file):
    bans_file = tmp_path / "bans.txt"
    bans_file.write_text("9.9.9.9\n")
    return Config(
        host="127.0.0.1",
        port=8000,
        heartbeat_expiry_minutes=30.0,
        hbcounter_cap=50000,
        hbcounter_rollover_drop=1000,
        purge_interval_seconds=3600.0,
        censors_path=str(censors_file),
        bans_path=str(bans_file),
        admin_token="s3cret",
    )


@pytest.fixture
async def admin_client(aiohttp_client, admin_config):
    app = create_app(config=admin_config, storage=InMemoryStorage())
    return await aiohttp_client(app)


async def test_censored_server_pretends_advertise_but_hidden(admin_client):
    resp = await admin_client.post(
        "/heartbeat",
        json={"ip": "1.2.3.4", "port": 27016, "name": "Best CASINO Ever"},
    )
    # Heartbeat looks successful to the operator.
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "Best CASINO Ever"
    # But the server never appears in the public listing.
    listing = await (await admin_client.get("/servers")).json()
    assert listing == []


async def test_censor_matches_description_too(admin_client):
    await admin_client.post(
        "/heartbeat",
        json={
            "ip": "1.2.3.4",
            "port": 27016,
            "name": "Polite Court",
            "description": "Click for FREE GEMS!!",
        },
    )
    assert await (await admin_client.get("/servers")).json() == []


async def test_clean_server_still_listed(admin_client):
    await admin_client.post(
        "/heartbeat",
        json={"ip": "1.2.3.4", "port": 27016, "name": "Polite Court"},
    )
    servers = await (await admin_client.get("/servers")).json()
    assert len(servers) == 1
    assert servers[0]["name"] == "Polite Court"


async def test_banned_ip_via_bans_file_rejected(admin_client):
    resp = await admin_client.post(
        "/heartbeat", json={"ip": "9.9.9.9", "port": 27016}
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "banned"


async def test_admin_endpoints_require_token(admin_client):
    resp = await admin_client.post("/admin/ban", json={"ip": "1.2.3.4"})
    assert resp.status == 401
    resp = await admin_client.post(
        "/admin/ban",
        json={"ip": "1.2.3.4"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status == 401


async def test_admin_ban_then_unban(admin_client):
    headers = {"Authorization": "Bearer s3cret"}
    # Register a server, then ban its IP -> kicked from listing.
    await admin_client.post("/heartbeat", json={"ip": "5.5.5.5", "port": 27016})
    resp = await admin_client.post(
        "/admin/ban",
        json={"ip": "5.5.5.5", "duration_minutes": 60, "reason": "spam"},
        headers=headers,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["banned"] == "5.5.5.5/32"
    assert body["kicked"] == 1
    # Heartbeat now rejected.
    resp = await admin_client.post(
        "/heartbeat", json={"ip": "5.5.5.5", "port": 27016}
    )
    assert resp.status == 403
    # Unban -> heartbeat works again.
    resp = await admin_client.delete(
        "/admin/ban", json={"ip": "5.5.5.5"}, headers=headers
    )
    assert (await resp.json())["removed"] is True
    resp = await admin_client.post(
        "/heartbeat", json={"ip": "5.5.5.5", "port": 27016}
    )
    assert resp.status == 200


async def test_admin_ban_permanent_when_no_duration(admin_client):
    headers = {"Authorization": "Bearer s3cret"}
    resp = await admin_client.post(
        "/admin/ban", json={"ip": "6.6.6.6"}, headers=headers
    )
    assert resp.status == 200
    listed = (await (await admin_client.get("/admin/bans", headers=headers)).json())["bans"]
    entry = next(e for e in listed if e["network"] == "6.6.6.6/32")
    assert entry["until"] is None
    assert entry["source"] == "admin"


async def test_admin_ban_cidr(admin_client):
    headers = {"Authorization": "Bearer s3cret"}
    await admin_client.post(
        "/admin/ban", json={"ip": "10.0.0.0/8"}, headers=headers
    )
    resp = await admin_client.post(
        "/heartbeat", json={"ip": "10.20.30.40", "port": 27016}
    )
    assert resp.status == 403


async def test_admin_kick_removes_server(admin_client):
    headers = {"Authorization": "Bearer s3cret"}
    await admin_client.post(
        "/heartbeat", json={"ip": "7.7.7.7", "port": 27016, "name": "A"}
    )
    await admin_client.post(
        "/heartbeat", json={"ip": "7.7.7.7", "port": 27017, "name": "B"}
    )
    # Kick a single ip:port leaves the other.
    resp = await admin_client.post(
        "/admin/kick", json={"ip": "7.7.7.7", "port": 27016}, headers=headers
    )
    assert (await resp.json())["removed"] == 1
    listing = await (await admin_client.get("/servers")).json()
    assert [s["port"] for s in listing] == [27017]
    # Kick the whole ip drops what's left.
    resp = await admin_client.post(
        "/admin/kick", json={"ip": "7.7.7.7"}, headers=headers
    )
    assert (await resp.json())["removed"] == 1
    assert await (await admin_client.get("/servers")).json() == []


async def test_admin_disabled_when_no_token(aiohttp_client, censors_file, tmp_path):
    cfg = Config(
        host="127.0.0.1",
        port=8000,
        heartbeat_expiry_minutes=30.0,
        hbcounter_cap=50000,
        hbcounter_rollover_drop=1000,
        purge_interval_seconds=3600.0,
        censors_path=str(censors_file),
        bans_path="",
        admin_token="",
    )
    app = create_app(config=cfg, storage=InMemoryStorage())
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/ban",
        json={"ip": "1.2.3.4"},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status == 503
