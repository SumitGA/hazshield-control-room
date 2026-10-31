# hazshield-control-room

This is the operator screen for HazShield AI, the hazard monitoring system I'm building for
mineral processing plants. The overview of the whole project, with the architecture and
links to the other repos, is in [HazshieldAI](https://github.com/SumitGA/HazshieldAI).

The control room shows a live wall of alarms, the incidents (episodes) behind them and the
isolation plans the workers write for each one. You can also start a short simulated gas
plume and watch the whole system react. It's running at
[app.sumitgautam.tech](https://app.sumitgautam.tech).

## Where it fits

The stream service doesn't detect or plan anything itself. It relays the live violation
feed that hazshield-ingest publishes to Redis, and reads the episodes and plans that
hazshield-worker writes to PostgreSQL:

```
ingest (Rust) --publish--> Redis pub/sub --> stream service --SSE--> React UI
workers ----------------> PostgreSQL ------------^   (REST)
```

It runs on the edge VM behind Caddy and the Cloudflare Tunnel set up in hazshield-infra.

## What's in here

```
hazshield-stream/      backend (Python, aiohttp)
  app.py               SSE feed, REST API, serves the built UI
  auth.py              operator sign-in: bcrypt passwords, sessions in Redis
  sim_control.py       simulation launcher: one at a time, parameters clamped
  deploy-stream.sh     builds the UI and deploys both to the edge VM
  hazshield-stream.service.example

hazshield-ui/          frontend (React 18 + Vite)
  src/App.jsx          alarm wall, live feed, stats
  src/PlansPanel.jsx   isolation plans as they arrive, with a detail drawer
  src/RateChart.jsx    violations per second, warn vs critical (operators only)
  src/SimLauncher.jsx  start a simulation from a preset or custom values
  src/AuthBar.jsx, src/useAuth.js, src/useLive.js
```

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/events` | live violations as Server-Sent Events |
| GET | `/api/topology` | plant layout: zones, adjacency, assets, sensors |
| GET | `/api/episodes` | current and recent alarm episodes |
| GET | `/api/plans/recent` | latest isolation plans |
| GET | `/api/plans/{alarm_id}` | the plan for one episode |
| GET | `/api/violations/recent` | recent violations |
| GET | `/api/violations/rate` | violations per second |
| GET | `/api/stats` | stream length, DLQ size, episode and plan counts, where plans came from |
| GET | `/api/sim/status` | whether a simulation is running |
| POST | `/api/sim/start` | start a simulation |
| POST | `/api/auth/login`, `/api/auth/logout` | sign in and out |
| GET | `/api/auth/me` | the signed-in operator |

## Why it's built this way

I went with Server-Sent Events rather than WebSockets because the control room only ever
listens. EventSource reconnects by itself, and I can debug the feed with plain `curl`.

The service keeps no state of its own. It relays Redis pub/sub and reads PostgreSQL, so I
can restart it whenever I like.

For sign-in I used server-side sessions in Redis with an httpOnly cookie instead of JWTs.
On a safety control surface, signing someone out has to take effect straight away, and
deleting the session in Redis does exactly that. Passwords are hashed with bcrypt.
Operators are rows in the `operator` table (migration 0006 in hazshield-ansible).

Anyone can watch the wall. The rate chart and the simulation launcher are only shown to
signed-in operators, and the actions I'm adding next, like acknowledging alarms, will need
sign-in too.

The server decides how big a simulation can be, not the browser. Only one runs at a time
(a lease with a heartbeat and a cooldown), and every value is clamped to what I've tested
the system with: 200 to 2,000 readings a second for 30 to 120 seconds, either a plume or a
plume plus flood. The presets are gentle, realistic and severe.

## Configuration

Set these in a systemd drop-in (`systemctl edit hazshield-stream`), never in the repo:

| Variable | Default | |
|---|---|---|
| `HAZ_DATABASE_URL` | local dev database | PostgreSQL connection string |
| `HAZ_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection string |
| `HAZ_HTTP_PORT` | `8010` | port Caddy proxies to |
| `HAZ_SESSION_TTL` | `43200` (12 hours, one shift) | how long a sign-in lasts, in seconds |
| `HAZ_SIM_PATH` | `/opt/hazshield-stream/sim.py` | simulator script |
| `HAZ_SIM_URL` | `http://127.0.0.1:8020` | ingest endpoint the simulator posts to |
| `HAZ_SIM_LEASE_TTL`, `HAZ_SIM_RENEW`, `HAZ_SIM_COOLDOWN` | `180`, `20`, `45` | simulation lease, heartbeat and cooldown, in seconds |

## Running it locally

You need PostgreSQL and Redis with the HazShield schema (see hazshield-ansible).

```bash
cd hazshield-stream
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
HAZ_DATABASE_URL=postgresql://... HAZ_REDIS_URL=redis://... ./venv/bin/python3 app.py
```

In another terminal, the UI with hot reload (it proxies `/api` and `/events` to port 8010):

```bash
cd hazshield-ui
npm install && npm run dev
```

## Deploying

```bash
./hazshield-stream/deploy-stream.sh
```

The script builds the UI, copies the backend and the build to `/opt/hazshield-stream` on
the edge VM, installs the Python dependencies in a virtualenv, restarts the service and
checks that `/api/stats` answers.

The first time, install `hazshield-stream.service.example` as
`/etc/systemd/system/hazshield-stream.service` and add the connection strings in a
drop-in. The unit runs as its own user with a read-only system, no access to home
directories, a private `/tmp` and a 256 MB memory limit.

## Checking it works

- [ ] `curl -s https://stream.<domain>/api/stats` returns JSON with stream and plan counts
- [ ] `curl -N https://stream.<domain>/events` prints violations while a simulation runs
- [ ] the wall loads, and signing in shows the rate chart and the launcher
- [ ] a realistic simulation lights up the wall, opens episodes and adds new plans
