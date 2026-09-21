"""Simulation trigger control v2 — single-flight lease + BOUNDED params.

Adds server-side parameter validation to the v1 lease mechanism. The UI
sends rate/duration/scenario, but the server is the authority: every value
is CLAMPED to a safe envelope before the sim runs. A malicious or buggy
client cannot request a flood — the worst it can do is ask for the max,
which the box is proven to handle.

The safe envelope (from observed behavior: rate 1500/dur 90 = ~2900
violations, box stayed responsive):
  rate     200..2000 readings/sec
  duration 30..120 seconds
  scenario one of {plume, plume+flood}

Presets map friendly names to (rate, duration) so users pick MEANING.
Everything else (the lease, heartbeat, guaranteed release, cooldown,
non-blocking subprocess) is unchanged from v1.
"""
import asyncio
import json
import os
import sys
import time
import uuid

import redis.asyncio as aredis

LEASE_KEY = "sim:lease"
COOLDOWN_KEY = "sim:cooldown"
PROGRESS_KEY = "sim:progress"   # NEW: live progress for the narration panel
LEASE_TTL_S = int(os.environ.get("HAZ_SIM_LEASE_TTL", "180"))
RENEW_EVERY_S = int(os.environ.get("HAZ_SIM_RENEW", "20"))
COOLDOWN_S = int(os.environ.get("HAZ_SIM_COOLDOWN", "45"))
SIM_PATH = os.environ.get("HAZ_SIM_PATH", "/opt/hazshield-stream/sim.py")
SIM_URL = os.environ.get("HAZ_SIM_URL", "http://127.0.0.1:8020")

# ---- the safe envelope: the server's authority ----
RATE_MIN, RATE_MAX = 200, 2000
DUR_MIN, DUR_MAX = 30, 120
SCENARIOS = {"plume", "plume+flood"}
PRESETS = {
    "gentle":    {"rate": 600,  "duration": 60,  "scenario": "plume"},
    "realistic": {"rate": 1200, "duration": 90,  "scenario": "plume"},
    "severe":    {"rate": 2000, "duration": 120, "scenario": "plume+flood"},
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def resolve_params(body: dict) -> dict:
    """Turn a client request into safe, clamped sim params. The client
    cannot escape the envelope no matter what it sends."""
    if body.get("preset") in PRESETS:
        return dict(PRESETS[body["preset"]])
    # advanced mode: clamp each field, fall back to realistic defaults
    try:
        rate = clamp(int(body.get("rate", 1200)), RATE_MIN, RATE_MAX)
    except (TypeError, ValueError):
        rate = 1200
    try:
        duration = clamp(int(body.get("duration", 90)), DUR_MIN, DUR_MAX)
    except (TypeError, ValueError):
        duration = 90
    scenario = body.get("scenario", "plume")
    if scenario not in SCENARIOS:
        scenario = "plume"
    return {"rate": rate, "duration": duration, "scenario": scenario}


class SimController:
    def __init__(self, redis_url, pg_dsn):
        self.redis_url = redis_url
        self.pg_dsn = pg_dsn
        self._proc = None
        self._task = None

    async def _redis(self):
        return aredis.from_url(self.redis_url, decode_responses=True)

    async def status(self):
        r = await self._redis()
        try:
            lease = await r.get(LEASE_KEY)
            if lease:
                lease_ttl = await r.ttl(LEASE_KEY)
                prog = await r.get(PROGRESS_KEY)
                out = {"state": "running", "run_id": lease,
                       "lease_ttl_s": max(0, lease_ttl)}
                if prog:
                    try:
                        out["progress"] = json.loads(prog)
                    except ValueError:
                        pass
                return out
            cd_ttl = await r.ttl(COOLDOWN_KEY)
            if cd_ttl and cd_ttl > 0:
                return {"state": "cooldown", "cooldown_s": cd_ttl}
            return {"state": "ready", "presets": PRESETS,
                    "bounds": {"rate": [RATE_MIN, RATE_MAX],
                               "duration": [DUR_MIN, DUR_MAX],
                               "scenarios": sorted(SCENARIOS)}}
        finally:
            await r.aclose()

    async def start(self, body: dict):
        params = resolve_params(body or {})
        run_id = uuid.uuid4().hex[:12]
        r = await self._redis()
        try:
            cd = await r.ttl(COOLDOWN_KEY)
            if cd and cd > 0:
                return False, {"error": "cooldown", "retry_after_s": cd}
            got = await r.set(LEASE_KEY, run_id, nx=True, ex=LEASE_TTL_S)
            if not got:
                held = await r.get(LEASE_KEY)
                return False, {"error": "already_running", "run_id": held}
            # seed initial progress so the UI narration has something at once
            await r.set(PROGRESS_KEY, json.dumps({
                "phase": "starting", "elapsed_s": 0, **params}), ex=LEASE_TTL_S)
        finally:
            await r.aclose()
        self._task = asyncio.create_task(self._supervise(run_id, params))
        return True, {"ok": True, "run_id": run_id, **params}

    async def _supervise(self, run_id, params):
        r = await self._redis()
        renew = asyncio.create_task(self._renew(r, run_id))
        prog = asyncio.create_task(self._progress(r, run_id, params))
        hard_timeout = params["duration"] + 40
        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, SIM_PATH,
                "--pg", self.pg_dsn, "--url", SIM_URL,
                "--rate", str(params["rate"]),
                "--duration", str(params["duration"]),
                "--scenario", params["scenario"],
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(
                    self._proc.communicate(), timeout=hard_timeout)
                if self._proc.returncode != 0:
                    print(json.dumps({"msg": "sim nonzero", "run_id": run_id,
                        "err": (err or b"")[:300].decode(errors="replace")}))
            except asyncio.TimeoutError:
                try:
                    self._proc.kill(); await self._proc.wait()
                except ProcessLookupError:
                    pass
        except Exception as e:
            print(json.dumps({"msg": "sim error", "run_id": run_id,
                              "error": f"{type(e).__name__}: {e}"}))
        finally:
            renew.cancel(); prog.cancel()
            try:
                await r.eval(
                    "if redis.call('get',KEYS[1])==ARGV[1] then redis.call('del',KEYS[1]) end "
                    "redis.call('del',KEYS[3]) "
                    "redis.call('set',KEYS[2],'1','EX',ARGV[2])",
                    3, LEASE_KEY, COOLDOWN_KEY, PROGRESS_KEY, run_id, str(COOLDOWN_S))
            except Exception:
                pass
            await r.aclose()
            self._proc = None

    async def _renew(self, r, run_id):
        try:
            while True:
                await asyncio.sleep(RENEW_EVERY_S)
                await r.eval("if redis.call('get',KEYS[1])==ARGV[1] then "
                             "return redis.call('expire',KEYS[1],ARGV[2]) end return 0",
                             1, LEASE_KEY, run_id, str(LEASE_TTL_S))
        except asyncio.CancelledError:
            pass

    async def _progress(self, r, run_id, params):
        """Write a phase + elapsed to Redis every second so the UI can
        narrate. The plume starts at 40% of duration (matches sim.py),
        so we can describe the phases in plain English without parsing
        sim.py's stdout."""
        dur = params["duration"]
        start = time.monotonic()
        ignite_at = dur * 0.40
        try:
            while True:
                elapsed = time.monotonic() - start
                if elapsed < ignite_at:
                    phase = "baseline"      # plant normal, readings flowing
                elif elapsed < ignite_at + 12:
                    phase = "igniting"      # the plume ramp is climbing
                elif elapsed < dur:
                    phase = "escalating"    # criticals crossing, plans firing
                else:
                    phase = "draining"      # sim done, generators catching up
                await r.set(PROGRESS_KEY, json.dumps({
                    "phase": phase, "elapsed_s": round(elapsed),
                    "duration_s": dur, **params}), ex=LEASE_TTL_S)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
