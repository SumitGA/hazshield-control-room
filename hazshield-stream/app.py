"""HazShield stream service — Phase 4.

The control room's single origin: relays the live violation firehose
as Server-Sent Events and answers REST queries for topology, episodes,
plans, and the ledger. Serves the built SPA from ./dist.

Positions:

1. SSE, NOT WEBSOCKETS. The control room only listens; the plant never
   takes commands from a browser. One-directional wants the simpler
   protocol: EventSource reconnects itself, plays nice with proxies
   and the Cloudflare tunnel, and is debuggable with curl.

2. THE SERVICE OWNS NO STATE. It relays pub/sub and reads Postgres.
   Kill it, restart it, run two of them: nothing is lost, because
   nothing lives here. Same cattle principle as the workers.

3. READ-ONLY BY CONSTRUCTION. Every DB statement is SELECT. The
   dashboard cannot ack, clear, or mutate. A public portfolio URL
   must not be a control surface.
"""
import asyncio
import json
import os
from pathlib import Path

import asyncpg
import redis.asyncio as aredis
from aiohttp import web

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


class Service:
    def __init__(self):
        self.redis_url = os.environ.get("HAZ_REDIS_URL", "redis://127.0.0.1:6379/0")
        self.pg_dsn = os.environ.get("HAZ_DATABASE_URL",
                                     "postgresql://hazshield:devpw@127.0.0.1/hazshield")
        self.port = int(os.environ.get("HAZ_HTTP_PORT", "8010"))
        self.clients: set[asyncio.Queue] = set()

    # ---- SSE fan-out -------------------------------------------------
    async def pump(self):
        """One pub/sub subscription feeding N connected browsers."""
        while True:
            try:
                r = aredis.from_url(self.redis_url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(CHANNEL)
                async for msg in ps.listen():
                    if msg["type"] != "message":
                        continue
                    for q in list(self.clients):
                        if q.qsize() < 500:      # slow browser? drop, don't block
                            q.put_nowait(msg["data"])
            except Exception:
                await asyncio.sleep(2)           # redis hiccup: reconnect

    async def events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"})
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

    # ---- REST --------------------------------------------------------
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

    async def stats(self, request):
        r = aredis.from_url(self.redis_url, decode_responses=True)
        xlen, dlq = await r.xlen(STREAM), await r.xlen(DLQ)
        await r.aclose()
        async with self.pool.acquire() as c:
            ledger = [dict(x) for x in await c.fetch(LEDGER_SQL)]
            episodes = await c.fetchval(
                "SELECT count(*) FROM alarm_events")
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

    # ---- lifecycle -----------------------------------------------------
    async def start(self):
        self.pool = await asyncpg.create_pool(self.pg_dsn, min_size=1, max_size=4)
        asyncio.get_event_loop().create_task(self.pump())
        app = web.Application()
        app.router.add_get("/events", self.events)
        app.router.add_get("/api/topology", self.topology)
        app.router.add_get("/api/episodes", self.episodes)
        app.router.add_get("/api/plans/{alarm_id}", self.plan)
        app.router.add_get("/api/stats", self.stats)
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
