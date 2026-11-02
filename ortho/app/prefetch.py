"""Download the busy places' sheets ahead, while nobody waits (the operator, 2026-11-02; recon pl04).

A first view of an area waits for its sheets, and ČÚZK gives a 77 MB sheet in 9 to 50 s, so the towns most views
will ask for are fetched when the service is idle: places.json (make_places.py: OpenStreetMap's cities and towns
of 20,000 or more), largest first, each the existing sheets within 2 km x sqrt(population / 10,000) of its centre
(3 to 12 km), nearest first, the whole plan cut at PREFETCH_GB. One download at a time, and only while no tile's
download is queued or running in any worker (store.queued): a viewer's sheets never wait behind these, and ČÚZK
sees one connection from us. A sheet that fails is recorded as any download's failure is and skipped.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from pyproj import Transformer

import sheets as S

PLACES = Path(__file__).with_name("places.json")
SHEET_BYTES = 77e6                                   # a sheet held: jp2 + sidecars (~76-77 MB, measured)
_to_tm33 = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


def radius_m(population: int) -> float:
    return min(12_000.0, max(3_000.0, 2_000.0 * math.sqrt(population / 10_000)))


def plan(known, budget_bytes: float, places_path: Path = PLACES) -> list[str]:
    """The sheets to fetch ahead, in order: by place (largest first), each place's nearest first; no sheet twice;
    as many as budget_bytes holds."""
    places = json.loads(places_path.read_text())["places"]
    out: list[str] = []
    seen: set[str] = set()
    for p in sorted(places, key=lambda p: -p["population"]):
        e, n = _to_tm33.transform(p["lon"], p["lat"])
        r = radius_m(p["population"])
        near = []
        for sid in S.ids_in_box((e - r, n - r, e + r, n + r), known, margin=0):
            e0, n0, e1, n1 = S.sheet_box(sid)
            d = math.hypot((e0 + e1) / 2 - e, (n0 + n1) / 2 - n)
            if d <= r + S.SHEET_M / math.sqrt(2) and sid not in seen:
                near.append((d, sid))
        for _, sid in sorted(near):
            seen.add(sid)
            out.append(sid)
    return out[:int(budget_bytes // SHEET_BYTES)]


def step(store, todo: list[str]) -> str | None:
    """Fetch the next sheet of the plan not held yet, if the service is idle: the one fetched (or tried), or None
    when there was nothing to do now (busy, full, or the plan is done)."""
    if store.queued() or store.full():
        return None
    for sid in todo:
        if not store.jp2(sid).exists() and not store.failed_lately(sid):
            store.download(sid, by="prefetch")
            return sid
    return None
