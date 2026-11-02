"""Surveyed heights a terrain release must agree with before it is registered (the operator, 2026-11-02).

earth.hillview.cz's first full build came out ellipsoidal, every height ~44 m high (GDAL 3.13 with PROJ's
network on turned the warp's Bpv into WGS 84; the builder warps with -novshift since), and it was registered
and put on prod and beta before anyone looked. So `earth release register terrain` samples each ČÚZK mosaic
at the summits in reference_heights.json and refuses when one reads more than TOLERANCE_M from its surveyed
height, or when a mosaic has no summit on it at all: nothing checked is not a pass. `--unchecked "<why>"`
registers without, and the release says so.

The summits are OSM's named peaks with a tagged ele (surveyed, Bpv) where the 10 m and 2 m bare AND surface
mosaics of terrain-cz-2026-11-02b all read within 1 m of it: open summits, no trees or towers lifting the
surface. One per cell of about 25 km, so a partial build meets some. A datum or unit slip moves every one of
them; one moved alone is a changed summit (a quarry, a new mast): the refusal names it, and it leaves the file.
"""
from __future__ import annotations

import json
import os

import numpy as np

from . import config

TOLERANCE_M = 5.0
# what a terrain release's tiles read of ČÚZK (config.dem_paths); GLO-30 is Copernicus's files as they come
MOSAICS = ("bare_vrt", "bare2_vrt", "dsm_vrt", "dsm2_vrt")
REFERENCE_FILE = os.path.join(os.path.dirname(__file__), "reference_heights.json")


class ReferenceError(ValueError):
    pass


def load(path: str | None = None) -> list[dict]:
    """The reference summits: [{name, lat, lon, ele}], ele orthometric (Bpv) metres."""
    with open(path or REFERENCE_FILE, encoding="utf-8") as f:
        return json.load(f)["summits"]


def check(points: list[dict] | None = None) -> dict:
    """Each mosaic at the summits it covers: {mosaic key: {points, median_m, max_abs_m}}, the offsets being the
    mosaic's height minus the surveyed one. Raises ReferenceError when a summit reads past TOLERANCE_M, or when a
    mosaic covers none."""
    from . import ground

    pts = load() if points is None else points
    lon = np.array([p["lon"] for p in pts], float)
    lat = np.array([p["lat"] for p in pts], float)
    ele = np.array([p["ele"] for p in pts], float)
    paths = config.dem_paths()
    out, off, bare = {}, [], []
    for k in MOSAICS:
        name = os.path.basename(paths[k])
        h = ground._sample_model(paths[k], lon, lat)
        on = np.flatnonzero(np.isfinite(h))
        if not len(on):
            bare.append(name)
            continue
        d = h[on] - ele[on]
        out[k] = {"points": len(on), "median_m": round(float(np.median(d)), 2),
                  "max_abs_m": round(float(np.abs(d).max()), 2)}
        off += [f"{name} at {pts[i]['name']} ({pts[i]['lat']}, {pts[i]['lon']}): {h[i]:.1f} m, surveyed {ele[i]:g} m"
                for i in on if abs(h[i] - ele[i]) > TOLERANCE_M]
    if off:
        raise ReferenceError(f"{len(off)} reference summit readings past {TOLERANCE_M:g} m (a datum or unit slip moves "
                             f"them all; one alone is a changed summit, to leave {os.path.basename(REFERENCE_FILE)}):\n  "
                             + "\n  ".join(off))
    if bare:
        raise ReferenceError(f"no reference summit on {', '.join(bare)}: nothing to check it by (a partial build, or "
                             f"not built here); register with --unchecked \"<why>\" to go without")
    return out
