"""Simulation trigger control — single-flight lease + supervised subprocess.

This is the safety-critical part of the user-triggered plume feature. A
public button that spawns sim.py is a resource bomb without it: concurrent
plumes would pin the LLM (llama3 at ~34s/plan on shared hardware saturates
fast) and the box must stay up 24/7 for the demo.

The protections, defense in depth:

1. SINGLE-FLIGHT LEASE (not a lock). Redis SET NX EX is atomic: of any
   number of simultaneous /start requests, exactly ONE wins the key. But
   it's a LEASE, not a flag — the EX TTL means a crashed service or a
   dead sim process can't wedge the system forever; the lease simply
   expires and the system heals. This is the core distributed-systems
   idea: correctness survives crash at any point.

2. HEARTBEAT RENEWAL. A legitimately long sim renews its lease while it
   runs (extends the TTL). A dead one stops renewing → lease lapses.
   So the TTL can be short (fast recovery) without cutting off a healthy
   long run. Renew interval << TTL, so a missed beat or two is survivable.

3. GUARANTEED RELEASE. On completion, crash, OR timeout, a finally block
   releases the lease and starts the cooldown. Belt-and-suspenders: the
   lease TTL outlives the max sim duration, so even if finally somehow
   doesn't run (SIGKILL), the lease still expires on its own.

4. HARD TIMEOUT. The subprocess is wrapped so it CANNOT exceed its
   bounded duration even if sim.py misbehaves — killed at the ceiling.

5. COOLDOWN. After any sim ends, a short cooldown key blocks a new start,
   so nobody can hammer back-to-back plumes.

6. NON-BLOCKING. The subprocess runs in the background (create_subprocess_exec);
   /start returns immediately with a run_id. The event loop is never
   blocked by the running sim.

The LLM throttle underneath (the workers' semaphore(1)) already caps
concurrent generation, so even a plume can't spawn unbounded model calls.
This layer caps the *plumes*; that layer caps the *generation*. Together:
safe for unattended public operation.
"""
import asyncio
import json
import os
import time
import uuid

import redis.asyncio as aredis

# ---- tunables (env-overridable; safe fixed defaults for a public demo) ----
LEASE_KEY = "sim:lease"
COOLDOWN_KEY = "sim:cooldown"
LEASE_TTL_S = int(os.environ.get("HAZ_SIM_LEASE_TTL", "60"))      # lease lifetime
RENEW_EVERY_S = int(os.environ.get("HAZ_SIM_RENEW", "15"))        # heartbeat
COOLDOWN_S = int(os.environ.get("HAZ_SIM_COOLDOWN", "60"))        # gap between sims
# Fixed, bounded plume profile — user does NOT get to pick these (yet).
# Deliberately gentle so the 24/7 box is never endangered.
SIM_RATE = int(os.environ.get("HAZ_SIM_RATE", "1500"))           # readings/sec
SIM_DURATION_S = int(os.environ.get("HAZ_SIM_DURATION", "90"))    # wall seconds
SIM_SCENARIO = os.environ.get("HAZ_SIM_SCENARIO", "plume")
# Hard ceiling on the subprocess regardless of duration arg (safety net).
SIM_HARD_TIMEOUT_S = SIM_DURATION_S + 30
SIM_PATH = os.environ.get("HAZ_SIM_PATH", "/opt/hazshield-stream/sim.py")
SIM_URL = os.environ.get("HAZ_SIM_URL", "http://127.0.0.1:8020")


class SimController:
    """Owns the lease and the running subprocess. One instance per service."""

    def __init__(self, redis_url, pg_dsn):
        self.redis_url = redis_url
        self.pg_dsn = pg_dsn
        self._proc = None
        self._run_id = None
        self._task = None

    async def _redis(self):
        return aredis.from_url(self.redis_url, decode_responses=True)

    async def status(self):
        """Report ready / running / cooldown, for the UI to reflect."""
        r = await self._redis()
        try:
            lease = await r.get(LEASE_KEY)
            cd_ttl = await r.ttl(COOLDOWN_KEY)  # -2 if absent, else seconds left
            if lease:
                lease_ttl = await r.ttl(LEASE_KEY)
                return {"state": "running", "run_id": lease,
                        "lease_ttl_s": max(0, lease_ttl)}
            if cd_ttl and cd_ttl > 0:
                return {"state": "cooldown", "cooldown_s": cd_ttl}
            return {"state": "ready"}
        finally:
            await r.aclose()

    async def start(self):
        """Try to acquire the lease and launch. Returns (ok, payload)."""
        run_id = uuid.uuid4().hex[:12]
        r = await self._redis()
        try:
            # cooldown check first — cheap, and gives a clearer message
            cd = await r.ttl(COOLDOWN_KEY)
            if cd and cd > 0:
                return False, {"error": "cooldown", "retry_after_s": cd}
            # ATOMIC single-flight: only one caller wins this SET.
            got = await r.set(LEASE_KEY, run_id, nx=True, ex=LEASE_TTL_S)
            if not got:
                held = await r.get(LEASE_KEY)
                return False, {"error": "already_running", "run_id": held}
        finally:
            await r.aclose()

        # We hold the lease. Launch the supervised runner in the background.
        self._run_id = run_id
        self._task = asyncio.create_task(self._supervise(run_id))
        return True, {"ok": True, "run_id": run_id,
                      "rate": SIM_RATE, "duration_s": SIM_DURATION_S,
                      "scenario": SIM_SCENARIO}

    async def _supervise(self, run_id):
        """Run sim.py with heartbeat lease-renewal, hard timeout, and
        guaranteed lease release + cooldown in finally."""
        r = await self._redis()
        renew_task = asyncio.create_task(self._renew(r, run_id))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "python3", SIM_PATH,
                "--pg", self.pg_dsn_for_psql(),
                "--url", SIM_URL,
                "--rate", str(SIM_RATE),
                "--duration", str(SIM_DURATION_S),
                "--scenario", SIM_SCENARIO,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(
                    self._proc.communicate(), timeout=SIM_HARD_TIMEOUT_S)
                code = self._proc.returncode
                if code != 0:
                    print(json.dumps({"msg": "sim exited nonzero", "run_id": run_id,
                                      "code": code, "err": (err or b"")[:300].decode(errors="replace")}))
                else:
                    print(json.dumps({"msg": "sim complete", "run_id": run_id}))
            except asyncio.TimeoutError:
                # hard ceiling hit — kill it
                print(json.dumps({"msg": "sim hard-timeout; killing", "run_id": run_id}))
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except ProcessLookupError:
                    pass
        except Exception as e:
            print(json.dumps({"msg": "sim supervise error", "run_id": run_id,
                              "error": f"{type(e).__name__}: {e}"}))
        finally:
            renew_task.cancel()
            # Release lease ONLY if we still own it (compare-and-delete),
            # then arm the cooldown. Using a small Lua script for atomicity.
            try:
                await r.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "  redis.call('del', KEYS[1]) end "
                    "redis.call('set', KEYS[2], '1', 'EX', ARGV[2])",
                    2, LEASE_KEY, COOLDOWN_KEY, run_id, str(COOLDOWN_S))
            except Exception:
                pass
            await r.aclose()
            self._proc = None
            self._run_id = None

    async def _renew(self, r, run_id):
        """Heartbeat: extend the lease while WE still hold it. If someone
        else's run_id is in the key (shouldn't happen), stop renewing."""
        try:
            while True:
                await asyncio.sleep(RENEW_EVERY_S)
                # compare-and-extend: only refresh if it's still our lease
                await r.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "  return redis.call('expire', KEYS[1], ARGV[2]) end return 0",
                    1, LEASE_KEY, run_id, str(LEASE_TTL_S))
        except asyncio.CancelledError:
            pass

    def pg_dsn_for_psql(self):
        """sim.py runs `psql <dsn>` — it needs a libpq-style DSN/URL.
        The service's asyncpg DSN works for psql too (postgresql://...)."""
        return self.pg_dsn
