# Nyan AO Master Server

An open-source [Attorney Online](https://attorneyonline.github.io/) master
server, written in Python with `aiohttp`.

A master server has two jobs:

1. **Serve the server list** — what monitoring bots and AO clients poll.
2. **Accept heartbeats** — what AO game servers send to register themselves.

This is a **clean-room reimplementation** of the *observable behaviour* of the
official AO master server (`AttorneyOnline/master`), which is closed source.
It is not a port of that code.

## Features

- `GET /servers` — JSON array of currently-registered servers.
- `POST /heartbeat` — register/refresh a server, keyed uniquely by `ip:port`.
- Ever-rising `hbcounter` per server, with rollover (50000 → 49000).
- Stale servers (no heartbeat for ~30 minutes) drop out of the listing,
  via both a background purge task and a lazy filter on read.
- Strict, untrusted-input validation on heartbeats.
- In-memory storage behind a `Storage` interface, so a SQLite backend can be
  swapped in later without touching the web layer.

## Requirements

- Python 3.9+
- `aiohttp` (see `requirements.txt`)

## Running it

```sh
pip install -r requirements.txt
python -m master_server
```

The server listens on `0.0.0.0:8000` by default.

### Configuration

All settings come from environment variables:

| Variable                       | Default   | Meaning                                        |
|---------------------------------|-----------|------------------------------------------------|
| `MS_HOST`                       | `0.0.0.0` | Listen host.                                   |
| `MS_PORT`                       | `8000`    | Listen port.                                   |
| `MS_HEARTBEAT_EXPIRY_MINUTES`   | `30`      | Minutes of silence before a server expires.    |
| `MS_HBCOUNTER_CAP`              | `50000`   | Value at which `hbcounter` rolls over.         |
| `MS_HBCOUNTER_ROLLOVER_DROP`    | `1000`    | How far `hbcounter` drops on rollover.         |
| `MS_PURGE_INTERVAL_SECONDS`     | `60`      | How often the background purge task runs.      |

## API

### `GET /servers`

Returns a JSON array of currently-registered (non-stale) servers.

Each object:

| Field         | Type            | Notes                                       |
|---------------|-----------------|---------------------------------------------|
| `ip`          | string          | Host or IP of the game server.              |
| `port`        | integer         | Game port.                                  |
| `name`        | string          | Display name.                               |
| `description` | string          | Free text.                                  |
| `players`     | integer         | Current player count.                       |
| `hbcounter`   | integer         | Ever-rising heartbeat counter.              |
| `ws_port`     | integer         | Plain WebSocket port for WebAO. *Omitted* when unset. |
| `wss_port`    | integer         | Secure WebSocket port for WebAO. *Omitted* when unset. |

`ws_port` / `wss_port` are omitted entirely when the server has none — no
`0`/`null` noise.

```sh
curl http://localhost:8000/servers
```

```json
[
  {
    "ip": "play.example.com",
    "port": 27016,
    "name": "Example Court",
    "description": "A test server",
    "players": 4,
    "hbcounter": 12,
    "ws_port": 2095
  }
]
```

### `POST /heartbeat`

Registers or refreshes a server. A server is identified uniquely by `ip:port`.
On each heartbeat the record is upserted, `last_seen` is set to now, and that
server's `hbcounter` is incremented by one (rolling over at the configured
cap). The stored record is returned.

Request body:

| Field         | Type    | Required | Notes                                  |
|---------------|---------|----------|----------------------------------------|
| `ip`          | string  | yes      | Non-empty.                             |
| `port`        | integer | yes      | 1–65535.                               |
| `name`        | string  | no       | Truncated to 256 chars.                |
| `description` | string  | no       | Truncated to 4096 chars.               |
| `players`     | integer | no       | Non-negative, default `0`.             |
| `ws_port`     | integer | no       | 1–65535, or omit / `0` / `null`.       |
| `wss_port`    | integer | no       | 1–65535, or omit / `0` / `null`.       |

Missing or invalid `ip`/`port` is rejected with `400` and a JSON `{"error": ...}`.

```sh
curl -X POST http://localhost:8000/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{
        "ip": "play.example.com",
        "port": 27016,
        "name": "Example Court",
        "description": "A test server",
        "players": 4,
        "ws_port": 2095
      }'
```

```json
{
  "ip": "play.example.com",
  "port": 27016,
  "name": "Example Court",
  "description": "A test server",
  "players": 4,
  "hbcounter": 1,
  "ws_port": 2095
}
```

## Using it with a monitoring bot

Point the bot's master-server URL at this service's listing endpoint:

```sh
MS_URL=https://your-host/servers
```

## Development

```sh
pip install -r requirements-dev.txt
pytest
```

Tests cover registration, heartbeat refresh, `hbcounter` increment and
rollover, stale-server expiry, and input validation.

## License

MIT — see [LICENSE](LICENSE).
