"""Rate limits: two budgets per client IP, and a cap on making.

1. READS of what exists are cheap -- a kept tile out of a pack. One view load is 200-400 tile
   requests in a few seconds, and flying is more: a generous token bucket per IP
   (EARTH_LIMIT_READ_RATE per second, bursts of EARTH_LIMIT_READ_BURST).
2. MAKES are expensive -- a tile cut from the mosaics: a small bucket per IP
   (EARTH_LIMIT_MAKE_RATE / EARTH_LIMIT_MAKE_BURST), and a cap on makes running at once across
   everyone (EARTH_LIMIT_MAKE_CONCURRENT, default the core count: making is CPU and disk).
3. Work only the UPSTREAM can price -- an orthophoto tile is cheap from the ortho service's cache and
   dear when it fetched sheets for it: charged to the same make bucket AFTER the answer, with what
   the upstream says it cost. The bucket may go into debt; while it owes, that address is refused.
   Asks of the ortho service in flight are capped per address (EARTH_LIMIT_ORTHO_PER_ADDRESS) under a
   cap across everyone (EARTH_LIMIT_ORTHO_CONCURRENT, what the ortho service serves at once): a tile
   whose sheets are still downloading takes a minute or two, and with one shared cap of 4 a single
   viewer's new area held every other viewer's cached tiles for 170 s (recon pl03, 2026-11-02).

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
import contextlib
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

    def _bucket(self, key: str) -> Bucket:
        """The client's bucket, refilled to now. Under self._lock."""
        now = self.clock()
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
        return b

    def _wait(self, b: Bucket, need: float) -> float:
        return (need - b.tokens) / self.rate if self.rate > 0 else math.inf

    def take(self, key: str, cost: float = 1.0) -> float:
        """0 when allowed (and the tokens are spent), else the seconds until it would be."""
        with self._lock:
            b = self._bucket(key)
            if b.tokens >= cost:
                b.tokens -= cost
                return 0.0
            return self._wait(b, cost)

    def owed(self, key: str) -> float:
        """0 unless the client is in debt (charge), else the seconds until it is not."""
        with self._lock:
            b = self._bucket(key)
            return 0.0 if b.tokens >= 0 else self._wait(b, 0.0)

    def charge(self, key: str, cost: float) -> None:
        """Spend `cost` after the fact, into debt if need be."""
        with self._lock:
            self._bucket(key).tokens -= cost


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


class AddressSlots:
    """At most `n` at once per address; more wait their turn (not refused: a view asks for its tiles all at
    once). Waiting is on the event loop, holding no thread. An address's entry goes when it has nothing in
    flight or waiting, so addresses seen once leave nothing behind."""

    def __init__(self, n: int):
        self.n = max(1, n)
        self._held: dict[str, list] = {}           # address -> [its semaphore, asks in flight or waiting]

    @contextlib.asynccontextmanager
    async def hold(self, address: str):
        e = self._held.setdefault(address, [asyncio.Semaphore(self.n), 0])
        e[1] += 1
        try:
            async with e[0]:
                yield
        finally:
            e[1] -= 1
            if e[1] == 0:
                del self._held[address]


class Limits:
    def __init__(self):
        self.read = Buckets(config.limit("READ_RATE", 100), config.limit("READ_BURST", 1000))
        self.make = Buckets(config.limit("MAKE_RATE", 5), config.limit("MAKE_BURST", 200))
        n = int(config.limit("MAKE_CONCURRENT", os.cpu_count() or 4))
        self.making = asyncio.Semaphore(max(1, n))
        self.making_n = max(1, n)
        # the ortho service: 4 workers x 8 threads there (ortho/app/config.py), some left for the overview
        self.ortho_address = AddressSlots(int(config.limit("ORTHO_PER_ADDRESS", 4)))
        self.ortho_upstream = asyncio.Semaphore(max(1, int(config.limit("ORTHO_CONCURRENT", 24))))

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

    def check_owed(self, request: Request) -> None:
        """Before work only the upstream can price: refused while the address owes makes."""
        if config.no_limits():
            return
        wait = self.make.owed(client_ip(request))
        if wait:
            raise HTTPException(429, f"too many tiles made for this address; retry in {math.ceil(wait)} s",
                                headers={"Retry-After": str(max(1, math.ceil(wait)))})

    def charge_make(self, request: Request, cost: float) -> None:
        """After it: what the upstream said the work cost, in tiles."""
        if config.no_limits() or cost <= 0:
            return
        self.make.charge(client_ip(request), cost)
