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

- `GET /servers` — JSON array of currently-registered servers, sorted by rank.
- `POST /heartbeat` — register/refresh a server, keyed uniquely by `ip:port`.
- `POST /servers` — alias for `POST /heartbeat` (akashi compatibility).
- **Ranking system** — servers are ranked by player count first, with heartbeat
  stability as a tiebreaker (see [Ranking](#ranking)).
- Clock-anchored `hbcounter` per server — minutes of master-verified uptime,
  with rollover (10080 → 9000).
- IP auto-detection when `ip` is omitted from heartbeat — reads
  `X-Forwarded-For` / `X-Real-IP` headers first, then falls back to the
  connecting IP. Works correctly behind nginx / Caddy.
- Stale servers (no heartbeat for ~60 minutes / 1 hour) drop out of the listing,
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

| Variable                      | Default  | Meaning                                                    |
|-------------------------------|----------|------------------------------------------------------------|
| `MS_HOST`                     | `0.0.0.0`| Listen host.                                               |
| `MS_PORT`                     | `8000`   | Listen port.                                               |
| `MS_HEARTBEAT_EXPIRY_MINUTES` | `60`     | Minutes of silence before a server is considered stale.    |
| `MS_HBCOUNTER_CAP`            | `10080`  | Value at which `hbcounter` rolls over (7 days at 1 hb/min).|
| `MS_HBCOUNTER_ROLLOVER_DROP`  | `1080`   | How far `hbcounter` drops on rollover (resets to 9000).    |
| `MS_PURGE_INTERVAL_SECONDS`   | `60`     | How often the background purge task runs.                  |

### Running behind a reverse proxy

If you put this server behind nginx, Caddy, or similar, make sure the proxy
forwards the real client IP so server advertisements show the correct address:

**nginx:**
```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

**Caddy:**
```caddy
reverse_proxy localhost:8000 {
    header_up X-Forwarded-For {remote_host}
}
```

## Ranking

`GET /servers` returns servers sorted **highest score first**. The score is:

```
score = players * 10081 + hbcounter
```

- **Players are the dominant factor.** One extra player always outranks any
  difference in heartbeat stability.
- **`hbcounter` is the tiebreaker.** Among servers with equal player counts,
  a server that has been online longer ranks higher.
- The multiplier `10081` is `hbcounter_cap + 1`, guaranteeing the above
  property holds at any counter value.

**Example ranking:**

| Rank | Server | Players | HBCounter | Score  |
|------|--------|---------|-----------|--------|
| 1    | A      | 6       | 0         | 60 486 |
| 2    | B      | 5       | 10 080    | 60 485 |
| 3    | C      | 5       | 2 880     | 53 285 |

Server A has one more player than B, so it ranks first despite having just
started. Server B has been online 7 days (max stability) versus C's 2 days,
so B ranks above C even though both have 5 players.

The `score` field is included in every server object so clients can display or
use it directly.

## HBCounter & rollover

`hbcounter` measures a server's uptime in **minutes**, counted against the
master server's own clock — not the number of heartbeats received:

- **Registration** sets the counter to `1`.
- Each later heartbeat advances it by the **whole minutes of real time** that
  have elapsed since the counter was last advanced. A heartbeat 5 minutes
  after the previous one adds `+5`; leftover seconds are carried forward so
  they accrue toward the next minute.
- Heartbeats arriving faster than once a minute add **nothing** — a server
  cannot inflate its counter (and its rank) by flooding the master with
  pings. The value reflects legitimate, master-verified uptime.
- **Cap:** `10080` — reached after exactly 7 days of uptime at 1 per minute.
- **Rollover:** when the cap is hit the counter drops to `9000` and keeps
  climbing. This prevents any server from holding a permanently insurmountable
  score lead purely from age.
- Both cap values are configurable via `MS_HBCOUNTER_CAP` and
  `MS_HBCOUNTER_ROLLOVER_DROP`.

## API

### `GET /servers`

Returns a JSON array of currently-registered, non-stale servers sorted by
rank (highest first).

Each object:

| Field         | Type    | Notes                                                  |
|---------------|---------|--------------------------------------------------------|
| `ip`          | string  | Host or IP of the game server.                         |
| `port`        | integer | Game port.                                             |
| `name`        | string  | Display name.                                          |
| `description` | string  | Free text.                                             |
| `players`     | integer | Current player count.                                  |
| `hbcounter`   | integer | Uptime in minutes, master-verified (max 10080, rolls to 9000).|
| `score`       | integer | Computed rank score (`players * 10081 + hbcounter`).   |
| `ws_port`     | integer | Plain WebSocket port for WebAO. *Omitted* when unset.  |
| `wss_port`    | integer | Secure WebSocket port for WebAO. *Omitted* when unset. |

```sh
curl http://localhost:8000/servers
```

```json
[
  {
    "ip": "play.example.com",
    "port": 27016,
    "name": "Example Court",
    "description": "A busy server",
    "players": 6,
    "hbcounter": 5040,
    "score": 65526,
    "ws_port": 2095
  },
  {
    "ip": "play.other.com",
    "port": 27016,
    "name": "Other Court",
    "description": "A stable but quieter server",
    "players": 5,
    "hbcounter": 10080,
    "score": 60485
  }
]
```

### `POST /heartbeat` / `POST /servers`

Registers or refreshes a server. Both endpoints are identical — `POST /servers`
exists for compatibility with **akashi**, which advertises there by default.

A server is identified uniquely by `ip:port`. On each heartbeat the record is
upserted, `last_seen` is updated, and that server's `hbcounter` is advanced by
the whole minutes of real time elapsed since it was last advanced (rolling
over at the cap). The stored record is returned.

**`ip` is optional.** If omitted or empty, the master server fills it in
automatically from the request's `X-Forwarded-For` / `X-Real-IP` headers, or
the connecting IP if those headers are absent.

Request body:

| Field         | Type    | Required | Notes                                          |
|---------------|---------|----------|------------------------------------------------|
| `ip`          | string  | no       | Defaults to the connecting IP if omitted.      |
| `port`        | integer | yes      | 1–65535.                                       |
| `name`        | string  | no       | Truncated to 256 chars.                        |
| `description` | string  | no       | Truncated to 4096 chars.                       |
| `players`     | integer | no       | Non-negative, default `0`.                     |
| `ws_port`     | integer | no       | 1–65535, or omit / `0` / `null`.               |
| `wss_port`    | integer | no       | 1–65535, or omit / `0` / `null`.               |

Missing or invalid `port` is rejected with `400` and a JSON `{"error": ...}`.

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
  "score": 40325,
  "ws_port": 2095
}
```

### Akashi configuration

In your akashi `config.ini`, point `serverlist_url` at this master server:

```ini
[General]
masterserver_ip   = your-host
server_name       = My AO Server
server_description = My server description
```

Akashi will POST to `/servers` automatically. If `server_domain` is not set,
the master server detects the IP from the connection.

## Using it with a monitoring bot

Point the bot's master-server URL at the listing endpoint:

```
https://your-host/servers
```

## Development

```sh
pip install -r requirements-dev.txt
pytest
```

Tests cover registration, heartbeat refresh, `hbcounter` time-based advance
and rollover, abuse resistance to rapid heartbeats, stale-server expiry, and
input validation.

## License

MIT — see [LICENSE](LICENSE).
