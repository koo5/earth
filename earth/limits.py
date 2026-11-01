"""Rate limits: two budgets per client IP, and a cap on making.

1. READS of what exists are cheap -- a kept tile out of a pack. One view load is 200-400 tile
   requests in a few seconds, and flying is more: a generous token bucket per IP
   (EARTH_LIMIT_READ_RATE per second, bursts of EARTH_LIMIT_READ_BURST).
2. MAKES are expensive -- a tile cut from the mosaics: a small bucket per IP
   (EARTH_LIMIT_MAKE_RATE / EARTH_LIMIT_MAKE_BURST), and a cap on makes running at once across
   everyone (EARTH_LIMIT_MAKE_CONCURRENT, default the core count: making is CPU and disk).

Past a limit: 429 with Retry-After, and a client keeps the coarser tile it already has.

From hillview's backend limiter (backend/api/app/rate_limiter.py): the client address -- believe
X-Forwarded-For only from the one proxy in front, and refuse one with several entries (header
injection) -- 429 with Retry-After, limits in the environment, a switch to turn them off for tests.
Not its sliding window: a list of timestamps per IP per hour is per-request work at tile rates,
and an hourly window is the wrong shape for bursts of 400 (its public-read default, 500 an hour,
is two view loads). Token buckets are O(1). State is in this process: one process is enough; a
second worker would need it shared (Redis).
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from . import config


class Bucket:
    __slots__ = ("tokens", "t")

    def __init__(self, tokens: float, t: float):
        self.tokens, self.t = tokens, t


class Buckets:
    """Token buckets keyed by client, the least recently used dropped past `max_keys` (a dropped
    client comes back with a full bucket: forgetting is generous, never punishing)."""

    def __init__(self, rate: float, burst: float, max_keys: int = 100_000, clock=time.monotonic):
        self.rate, self.burst, self.max_keys, self.clock = rate, burst, max_keys, clock
        self._b: OrderedDict[str, Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: str, cost: float = 1.0) -> float:
        """0 when allowed (and the tokens are spent), else the seconds until it would be."""
        now = self.clock()
        with self._lock:
            b = self._b.get(key)
            if b is None:
                b = Bucket(self.burst, now)
                self._b[key] = b
                if len(self._b) > self.max_keys:
                    self._b.popitem(last=False)
            else:
                self._b.move_to_end(key)
                b.tokens = min(self.burst, b.tokens + (now - b.t) * self.rate)
                b.t = now
            if b.tokens >= cost:
                b.tokens -= cost
                return 0.0
            return (cost - b.tokens) / self.rate if self.rate > 0 else math.inf


def client_ip(request: Request) -> str:
    """The client's address. X-Forwarded-For is believed only from a trusted proxy (Caddy, the one
    in front), and only with a single entry: several mean someone added their own."""
    peer = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if not fwd or peer not in config.trusted_proxies():
        return peer
    if "," in fwd:
        raise HTTPException(403, "access denied")
    return fwd.strip()


class Limits:
    def __init__(self):
        self.read = Buckets(config.limit("READ_RATE", 100), config.limit("READ_BURST", 1000))
        self.make = Buckets(config.limit("MAKE_RATE", 5), config.limit("MAKE_BURST", 200))
        n = int(config.limit("MAKE_CONCURRENT", os.cpu_count() or 4))
        self.making = asyncio.Semaphore(max(1, n))
        self.making_n = max(1, n)

    def _check(self, buckets: Buckets, request: Request, what: str, cost: float = 1.0) -> None:
        if config.no_limits():
            return
        wait = buckets.take(client_ip(request), cost)
        if wait:
            raise HTTPException(429, f"too many {what} from this address; retry in {math.ceil(wait)} s",
                                headers={"Retry-After": str(max(1, math.ceil(wait)))})

    def check_read(self, request: Request) -> None:
        self._check(self.read, request, "requests")

    def check_make(self, request: Request, cost: float = 1.0) -> None:
        """One tile made costs 1; other work says what it costs in tiles (a ground query: its
        points over a tile's 4,225)."""
        self._check(self.make, request, "tiles to make", cost)
