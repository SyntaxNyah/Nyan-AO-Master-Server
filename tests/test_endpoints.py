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


async def test_heartbeat_refresh_increments_hbcounter(client):
    payload = {"ip": "1.2.3.4", "port": 27016}
    first = await (await client.post("/heartbeat", json=payload)).json()
    second = await (await client.post("/heartbeat", json=payload)).json()
    assert first["hbcounter"] == 1
    assert second["hbcounter"] == 2

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
