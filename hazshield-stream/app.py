"""HazShield stream service — Phase 4 + Phase A (operator auth).

The control room's origin: relays the live violation firehose as SSE,
answers REST queries for topology/episodes/plans/ledger, serves the SPA,
triggers bounded simulations, and (Phase A) authenticates operators.

Positions:

1. SSE, NOT WEBSOCKETS. The control room only listens for live data;
   EventSource reconnects itself and is debuggable with curl.

2. THE SERVICE OWNS NO STATE (that it can't rebuild). It relays pub/sub
   and reads Postgres. Sessions live in Redis, not here.

3. THE OBSERVE/ACT BOUNDARY. Reads are public. ACTIONS (Phase B+) require
   an authenticated operator. Auth is server-side sessions in Redis +
   httpOnly cookies, NOT JWTs — a safety control surface wants instant
   revocability over statelessness, and we already run Redis.
"""
import asyncio
import json
import os
from pathlib import Path

import asyncpg
import redis.asyncio as aredis
from aiohttp import web
from sim_control import SimController
from auth import Sessions, verify_password, COOKIE_NAME

CHANNEL = "hazshield:violations:live"
STREAM = "hazshield:violations"
DLQ = "hazshield:violations:dlq"
DIST = Path(__file__).parent / "dist"

TOPOLOGY_SQL = """
SELECT z.zone_id, z.name, z.kind, s.name AS site,
       count(sn.sensor_id) AS sensors
FROM zone z
JOIN site s USING (site_id)
LEFT JOIN sensor sn USING (zone_id)
GROUP BY z.zone_id, z.name, z.kind, s.name
ORDER BY s.name, z.name
"""

ADJACENCY_SQL = """
SELECT zone_id, adjacent_id, link_kind FROM zone_adjacency
"""

EPISODES_SQL = """
SELECT a.alarm_id, a.sensor_id, a.zone_id, z.name AS zone_name,
       s.kind AS sensor_kind, a.severity::text, a.state::text,
       a.peak_value, a.n_readings,
       extract(epoch FROM now() - a.opened_at)::int AS age_s,
       a.cleared_at IS NOT NULL AS cleared,
       p.status::text AS plan_status, p.model AS plan_model
FROM alarm_events a
JOIN zone z USING (zone_id)
JOIN sensor s USING (sensor_id)
LEFT JOIN LATERAL (
    SELECT status, model FROM isolation_plans
    WHERE alarm_id = a.alarm_id ORDER BY created_at DESC LIMIT 1
) p ON true
WHERE a.state <> 'cleared' OR a.cleared_at > now() - interval '10 minutes'
ORDER BY a.state <> 'cleared' DESC, a.opened_at DESC
LIMIT 100
"""

PLAN_SQL = """
SELECT plan_id, alarm_id, model, status::text, latency_ms, plan, created_at
FROM isolation_plans WHERE alarm_id = $1
ORDER BY created_at DESC LIMIT 1
"""

LEDGER_SQL = """
SELECT model, status::text, count(*)::int AS n,
       coalesce(round(avg(latency_ms)), 0)::int AS avg_ms
FROM isolation_plans GROUP BY model, status ORDER BY n DESC
"""

RECENT_PLANS_SQL = """
SELECT p.plan_id, p.alarm_id, p.model, p.status::text, p.latency_ms,
       p.plan, p.created_at,
       z.name AS zone_name, s.kind::text AS sensor_kind
FROM isolation_plans p
JOIN alarm_events a ON a.alarm_id = p.alarm_id
JOIN zone z ON z.zone_id = a.zone_id
JOIN sensor s ON s.sensor_id = a.sensor_id
WHERE p.status IN ('ready','fallback') AND p.plan IS NOT NULL
ORDER BY p.created_at DESC
LIMIT 8
"""

# ---- Phase A: auth SQL ----
OPERATOR_BY_NAME_SQL = ("SELECT operator_id, username, display_name, role, pw_hash "
                        "FROM operator WHERE username = $1")
TOUCH_LOGIN_SQL = "UPDATE operator SET last_login = now() WHERE operator_id = $1"


class Service:
    def __init__(self):
        self.redis_url = os.environ.get("HAZ_REDIS_URL", "redis://127.0.0.1:6379/0")
        self.pg_dsn = os.environ.get("HAZ_DATABASE_URL",
                                     "postgresql://hazshield:devpw@127.0.0.1/hazshield")
        self.port = int(os.environ.get("HAZ_HTTP_PORT", "8010"))
        self.clients: set[asyncio.Queue] = set()
        self.sim = SimController(self.redis_url, self.pg_dsn)
        self.sessions = Sessions(self.redis_url)   # Phase A

    # ---- sim ----
    async def sim_status(self, request):
        return web.json_response(await self.sim.status())

    async def sim_start(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, payload = await self.sim.start(body)
        return web.json_response(payload, status=(202 if ok else 429))

    # ---- Phase A: auth ----
    async def _current_operator(self, request):
        """Who is this request from? Reads the session cookie -> Redis.
        Returns the operator dict or None. Every protected endpoint (B+)
        will call this and 401 if it's None."""
        token = request.cookies.get(COOKIE_NAME)
        return await self.sessions.get(token)

    async def login(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        async with self.pool.acquire() as c:
            row = await c.fetchrow(OPERATOR_BY_NAME_SQL, username)
        if not row or not verify_password(password, row["pw_hash"]):
            return web.json_response({"error": "invalid credentials"}, status=401)
        operator = {k: (str(v) if k == "operator_id" else v)
                    for k, v in dict(row).items() if k != "pw_hash"}
        token = await self.sessions.create(operator)
        async with self.pool.acquire() as c:
            await c.execute(TOUCH_LOGIN_SQL, row["operator_id"])
        resp = web.json_response({"ok": True, "operator": operator})
        resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=True,
                        samesite="Lax", max_age=43200, path="/")
        return resp

    async def logout(self, request):
        token = request.cookies.get(COOKIE_NAME)
        await self.sessions.destroy(token)
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    async def me(self, request):
        op = await self._current_operator(request)
        return web.json_response({"operator": op})

    # ---- SSE fan-out ----
    async def pump(self):
        """One pub/sub subscription feeding N connected browsers.
        Each iteration owns its connection and closes it cleanly on error,
        so a hiccup can't leave a half-open listen() generator wedged."""
        while True:
            r = None
            ps = None
            try:
                r = aredis.from_url(self.redis_url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(CHANNEL)
                async for msg in ps.listen():
                    if msg["type"] != "message":
                        continue
                    for q in list(self.clients):
                        if q.qsize() < 500:
                            q.put_nowait(msg["data"])
            except Exception as e:
                print(json.dumps({"msg": "pump error; reconnecting",
                                  "error": f"{type(e).__name__}: {str(e)[:120]}"}))
            finally:
                try:
                    if ps is not None:
                        await ps.unsubscribe(CHANNEL)
                        await ps.aclose()
                except Exception:
                    pass
                try:
                    if r is not None:
                        await r.aclose()
                except Exception:
                    pass
                await asyncio.sleep(2)

    async def events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"event: violation\ndata: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": heartbeat\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(q)
        return resp

    # ---- REST reads ----
    async def topology(self, request):
        async with self.pool.acquire() as c:
            zones = [dict(r) for r in await c.fetch(TOPOLOGY_SQL)]
            adj = [dict(r) for r in await c.fetch(ADJACENCY_SQL)]
        return web.json_response({"zones": zones, "adjacency": adj},
                                 dumps=lambda o: json.dumps(o, default=str))

    async def episodes(self, request):
        async with self.pool.acquire() as c:
            rows = [dict(r) for r in await c.fetch(EPISODES_SQL)]
        return web.json_response(rows, dumps=lambda o: json.dumps(o, default=str))

    async def plan(self, request):
        async with self.pool.acquire() as c:
            row = await c.fetchrow(PLAN_SQL, request.match_info["alarm_id"])
        if not row:
            return web.json_response({"error": "no plan for this alarm"}, status=404)
        d = dict(row)
        d["plan"] = json.loads(d["plan"]) if d["plan"] else None
        return web.json_response(d, dumps=lambda o: json.dumps(o, default=str))

    async def recent_plans(self, request):
        async with self.pool.acquire() as c:
            rows = [dict(r) for r in await c.fetch(RECENT_PLANS_SQL)]
        for r in rows:
            if isinstance(r.get("plan"), str):
                r["plan"] = json.loads(r["plan"])
        return web.json_response(rows, dumps=lambda o: json.dumps(o, default=str))

    async def recent_violations(self, request):
        """Poll-based live feed (SSE is buffered by Cloudflare). Returns
        the tail of the violations stream — the browser polls this every
        ~1.5s instead of holding a streamed connection."""
        r = aredis.from_url(self.redis_url, decode_responses=True)
        try:
            # XREVRANGE returns newest-first; take the last 30
            entries = await r.xrevrange(STREAM, count=30)
        finally:
            await r.aclose()
        out = []
        for entry_id, fields in entries:
            v = fields.get("v")
            if v:
                try:
                    d = json.loads(v)
                    d["_id"] = entry_id
                    out.append(d)
                except ValueError:
                    pass
        return web.json_response(out)

    async def violations_rate(self, request):
        """Per-second violation rate for the last ~90s, split warn/critical.
        Drives the live rate chart. Reads the stream tail, buckets by second."""
        import time as _t
        r = aredis.from_url(self.redis_url, decode_responses=True)
        try:
            entries = await r.xrevrange(STREAM, count=4000)
        finally:
            await r.aclose()
        now = int(_t.time())
        window = 90
        buckets = {}
        for entry_id, fields in entries:
            v = fields.get("v")
            if not v:
                continue
            try:
                d = json.loads(v)
            except ValueError:
                continue
            try:
                ms = int(entry_id.split("-")[0])
            except (ValueError, IndexError):
                continue
            sec = ms // 1000
            age = now - sec
            if age < 0 or age >= window:
                continue
            b = buckets.setdefault(sec, {"warn": 0, "critical": 0})
            sev = d.get("severity", "warn")
            if sev in b:
                b[sev] += 1
        series = []
        for age in range(window - 1, -1, -1):
            sec = now - age
            b = buckets.get(sec, {"warn": 0, "critical": 0})
            series.append({"t": -age, "warn": b["warn"], "critical": b["critical"]})
        return web.json_response(series)

    async def stats(self, request):
        r = aredis.from_url(self.redis_url, decode_responses=True)
        xlen, dlq = await r.xlen(STREAM), await r.xlen(DLQ)
        await r.aclose()
        async with self.pool.acquire() as c:
            ledger = [dict(x) for x in await c.fetch(LEDGER_SQL)]
            episodes = await c.fetchval("SELECT count(*) FROM alarm_events")
            open_now = await c.fetchval(
                "SELECT count(*) FROM alarm_events WHERE state <> 'cleared'")
            plans = await c.fetchval("SELECT count(*) FROM isolation_plans")
            gens = await c.fetchval(
                "SELECT count(*) FROM isolation_plans WHERE model NOT IN "
                "('cache','rulebook','unassigned')")
        return web.json_response({
            "stream_len": xlen, "dlq_len": dlq, "episodes_total": episodes,
            "episodes_open": open_now, "plans_total": plans,
            "generations": gens, "ledger": ledger})

    # ---- lifecycle ----
    async def start(self):
        self.pool = await asyncpg.create_pool(self.pg_dsn, min_size=1, max_size=4)
        asyncio.get_event_loop().create_task(self.pump())
        app = web.Application()
        app.router.add_get("/events", self.events)
        app.router.add_get("/api/topology", self.topology)
        app.router.add_get("/api/episodes", self.episodes)
        app.router.add_get("/api/plans/recent", self.recent_plans)
        app.router.add_get("/api/plans/{alarm_id}", self.plan)
        app.router.add_get("/api/violations/recent", self.recent_violations)
        app.router.add_get("/api/violations/rate", self.violations_rate)
        app.router.add_get("/api/stats", self.stats)
        app.router.add_get("/api/sim/status", self.sim_status)
        app.router.add_post("/api/sim/start", self.sim_start)
        # Phase A: auth
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_post("/api/auth/logout", self.logout)
        app.router.add_get("/api/auth/me", self.me)
        if DIST.exists():
            app.router.add_get("/", lambda r: web.FileResponse(DIST / "index.html"))
            app.router.add_static("/assets", DIST / "assets")
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.port).start()
        print(json.dumps({"msg": "stream service up", "port": self.port}))
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(Service().start())
