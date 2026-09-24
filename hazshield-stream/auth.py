"""Operator authentication — Phase A.

DESIGN DECISION: server-side sessions in Redis + httpOnly cookies, NOT
JWTs. For a safety control surface, instant revocability beats
statelessness (delete the session = instant logout), httpOnly resists
XSS token theft, and we already run Redis. JWTs would win only with many
independent services verifying tokens without a shared store; we don't.

Passwords: bcrypt (deliberately slow, brute-force resistant).
"""
import json
import os
import secrets
import time

import bcrypt
import redis.asyncio as aredis

SESSION_PREFIX = "sess:"
SESSION_TTL_S = int(os.environ.get("HAZ_SESSION_TTL", "43200"))  # 12h shift
COOKIE_NAME = "haz_session"


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), pw_hash.encode())
    except (ValueError, TypeError):
        return False


class Sessions:
    """Redis-backed sessions. A random opaque token maps to operator
    identity with a TTL. Delete = instant logout."""

    def __init__(self, redis_url):
        self.redis_url = redis_url

    async def _r(self):
        return aredis.from_url(self.redis_url, decode_responses=True)

    async def create(self, operator: dict) -> str:
        token = secrets.token_urlsafe(32)
        r = await self._r()
        try:
            await r.set(SESSION_PREFIX + token, json.dumps({
                "operator_id": operator["operator_id"],
                "username": operator["username"],
                "display_name": operator["display_name"],
                "role": operator["role"],
                "created": int(time.time()),
            }), ex=SESSION_TTL_S)
        finally:
            await r.aclose()
        return token

    async def get(self, token: str):
        if not token:
            return None
        r = await self._r()
        try:
            raw = await r.get(SESSION_PREFIX + token)
            if not raw:
                return None
            await r.expire(SESSION_PREFIX + token, SESSION_TTL_S)  # sliding
            return json.loads(raw)
        finally:
            await r.aclose()

    async def destroy(self, token: str):
        if not token:
            return
        r = await self._r()
        try:
            await r.delete(SESSION_PREFIX + token)
        finally:
            await r.aclose()
