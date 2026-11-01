"""Terrain tiles: the ground as a surface, in pieces a viewer asks for by where it is looking.

A tile is the standard web-map (slippy) tile z/x/y; its heights are an N x N grid laid evenly in
that tile's Mercator square, so two neighbours at one zoom share their edge samples and meet
without a crack. Heights are served ELLIPSOIDAL (datum.py), so a client needs no geoid.

Where each zoom's heights come from:
  - zoom <= GLOBAL_MAX_Z: the coarse global model alone (ETOPO 2022, ~1.8 km cells; a zoom-7 tile
    is ~300 km wide). Sea, and the rare land below sea level, is drawn at 0 m ("sea").
  - finer: ground.sample_ground -- bare (ČÚZK DTM10 only) or surface (ČÚZK DSM10 where built,
    else GLO-30) -- with the global model under whatever those do not cover;
  - zoom >= FINE_Z (6 m cells and finer): the 2 m mosaics wherever they are built.
Bare earth has holes inside the ground ČÚZK covers: lidar sees a roof, not the ground under it,
and nothing on water. A hole is filled from the bare ground around it (ground.filled_heights;
sources "dtm10.fill" / "dtm2.fill"), never from the global model: the global model is for ground ČÚZK
does not cover. It used to fill them, and at 1.8 km a cell it is metres off the ground there --
under a 40 m supermarket in Brandýs it stood 4 m above the car park, a flat block in the shape of
the building, with the orthophoto's roof stretched over it (2026-10-15).
Every sample carries a SOURCE byte (SOURCES), which is how a client knows where the data is no
finer than the tile, and how the attribution knows what it is attributing.

COMPLETE = every sample has a height and none is waiting on data that could still arrive: a ČÚZK
sheet in the index but not built, a GLO-30 tile not fetched, at a zoom where that data would be
used. Only a complete tile is kept (store.py); an incomplete one is answered and made again next
time. A coarse tile drawn from the global model is final, so it is complete: an ocean tile used to
stay incomplete for ever and a client never settled.

Wire formats: see `encode_f32` and `encode_q16`.

(From hillview's workbench, 2026-10-13.)
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field

import numpy as np

from . import config, datum, ground

N = 65
MAX_Z = 18
FINE_Z = int(os.getenv("EARTH_TILE_FINE_Z", "16"))
GLOBAL_MAX_Z = int(os.getenv("EARTH_TILE_GLOBAL_MAX_Z", "7"))
# below these zooms, missing ČÚZK sheets and missing GLO-30 tiles do not make a tile incomplete:
# a tile that coarse covers too much ground for them to be fetched on its account, and the global
# model under it is the answer at that zoom
CUZK_MIN_Z = int(os.getenv("EARTH_TILE_CUZK_MIN_Z", "13"))
GLO30_MIN_Z = int(os.getenv("EARTH_TILE_GLO30_MIN_Z", "10"))

# the source byte per sample; 0 is no ground. Names are the mosaics' file names, as the workbench
# has always sent them, so a client of either reads the same numbers.
SOURCES = {None: 0, "dtm10.vrt": 1, "dsm10.vrt": 2, "glo30.vrt": 3, "dtm2.vrt": 4, "dsm2.vrt": 5,
           "etopo60": 6, "sea": 7, "dtm10.fill": 8, "dtm2.fill": 9}
SOURCE_NAMES = {v: k for k, v in SOURCES.items() if k}
MODELS = ("surface", "bare")

# what makes the tiles, in every release's source stamp: a new way of making them is new tiles
GENERATOR = f"tiles-2 bare-fill {ground.FILL_REACH_M:g} m"


class TileError(Exception):
    """The tile cannot be made here (a configuration problem: an index, a geoid grid). Becomes 503."""


def valid(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_Z and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(w, s, e, n) degrees of a slippy tile."""
    k = 2 ** z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / k))))  # noqa: E731
    return x / k * 360 - 180, lat(y + 1), (x + 1) / k * 360 - 180, lat(y)


def tile_grid(z: int, x: int, y: int, n: int = N):
    """The sample lon/lat grid, row-major from the north-west corner, even in Mercator."""
    k = 2 ** z
    t = np.linspace(0.0, 1.0, n)
    lons = (x + t) / k * 360 - 180
    lats = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + t) / k))))
    LON, LAT = np.meshgrid(lons, lats)
    return LON.ravel(), LAT.ravel()


def global_heights(path: str, lons, lats):
    """(orthometric heights, source bytes) from the global model: land as it is, sea and below-sea
    ground at 0 m marked "sea", NaN / 0 where the model is missing."""
    import rasterio
    # not ground._sample_model: it takes anything at or below -1000 m for a DEM's nodata, and the
    # global model's sea floor is real data down to -10 km. Only the file's own nodata is missing.
    H = np.full(len(lons), np.nan)
    if len(lons) and os.path.exists(path):
        with rasterio.open(path) as ds:
            # a hair more than half a cell inside the edges: samples sit at cell centres, and a
            # point ON the 180th meridian or a pole has no neighbour to interpolate with
            hx, hy = abs(ds.res[0]) * 0.5005, abs(ds.res[1]) * 0.5005
            lo = np.clip(np.asarray(lons, float), ds.bounds.left + hx, ds.bounds.right - hx)
            la = np.clip(np.asarray(lats, float), ds.bounds.bottom + hy, ds.bounds.top - hy)
            H = np.asarray(ground.bilinear(ds, lo, la), float)
            if ds.nodata is not None:
                H[np.isclose(H, ds.nodata)] = np.nan
            H[H < -12_000] = np.nan
    src = np.where(np.isfinite(H), np.where(H > 0, SOURCES["etopo60"], SOURCES["sea"]), 0).astype("u1")
    return np.where(np.isfinite(H), np.maximum(H, 0.0), np.nan), src


def encode_f32(h, src) -> bytes:
    """float32 heights, row-major from the north-west corner (NaN where there is no ground), then
    one source byte per sample."""
    return np.asarray(h, "<f4").tobytes() + np.asarray(src, "u1").tobytes()


def encode_q16(h, src) -> bytes | None:
    """A tile as decimetres: b"HVQ1", the float64 base (the lowest height), then per sample an
    int16 -- row by row, each row's first sample against the previous row's first, every other
    sample against its left neighbour -- then the source bytes. A sample with no ground (source 0)
    repeats its neighbour, so the deltas stay small. Quantised to 5 cm at worst; gzips to 1-5 KB
    where float32 gzips to ~13 KB. None when a step does not fit in an int16 (3.2 km between
    neighbours), and the caller sends float32."""
    n = int(round(len(h) ** 0.5))
    v = np.asarray(h, float).reshape(n, n).copy()
    ok = np.isfinite(v)
    if not ok.any():
        base = 0.0
        q = np.zeros((n, n), np.int64)
    else:
        base = float(np.nanmin(v))
        # fill the holes along each row from the left (then the right), so the deltas stay small
        for r in range(n):
            row, m = v[r], ok[r]
            if not m.any():
                row[:] = v[r - 1] if r else base
                continue
            idx = np.where(m, np.arange(n), 0)
            np.maximum.accumulate(idx, out=idx)
            first = int(np.argmax(m))
            idx[:first] = first
            row[:] = row[idx]
        q = np.round((v - base) * 10).astype(np.int64)
    d = np.empty_like(q)
    d[:, 1:] = q[:, 1:] - q[:, :-1]
    d[0, 0] = q[0, 0]
    d[1:, 0] = q[1:, 0] - q[:-1, 0]
    if np.abs(d).max(initial=0) > 32767:
        return None
    return b"HVQ1" + struct.pack("<d", base) + d.astype("<i2").tobytes() + np.asarray(src, "u1").tobytes()


def decode_q16(body: bytes, n: int = N):
    """(ellipsoidal heights with NaN where no ground, source bytes): the client's arithmetic."""
    assert body[:4] == b"HVQ1"
    base = struct.unpack("<d", body[4:12])[0]
    d = np.frombuffer(body[12:12 + 2 * n * n], "<i2").astype(np.int64).reshape(n, n)
    src = np.frombuffer(body[12 + 2 * n * n:], "u1").reshape(n, n)
    q = np.empty_like(d)
    q[:, 0] = np.cumsum(d[:, 0])
    q[:, 1:] = d[:, 1:]
    q = np.cumsum(q, axis=1)
    h = base + q / 10.0
    return np.where(src == 0, np.nan, h), src


@dataclass
class Tile:
    z: int
    x: int
    y: int
    model: str
    heights: np.ndarray                    # ellipsoidal, float64, N*N, NaN where no ground
    src: np.ndarray                        # source bytes, N*N
    # what each sample's ground said (ground.GROUND_STATUSES), coarse tiles: ok / missing
    counts: dict = field(default_factory=dict)
    # why the tile is not complete: "cuzk" (a sheet in the index not built), "glo30" (a tile not
    # fetched), "nodata" (samples with no height at all)
    waiting: list = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.waiting

    def used(self) -> dict[str, int]:
        """source name -> samples, of what is SENT (the 2 m pass overrides the ground's own answer)"""
        return {SOURCE_NAMES[int(c)]: int(k) for c, k in zip(*np.unique(self.src, return_counts=True))
                if int(c) in SOURCE_NAMES}

    def encode(self, fmt: str) -> tuple[bytes, str]:
        """(body, format actually sent): q16 falls back to f32 where it cannot hold the tile"""
        h = self.heights.astype("<f4")
        if fmt == "q16":
            body = encode_q16(h, self.src)
            if body is not None:
                return body, "q16"
        return encode_f32(h, self.src), "f32"


def make(z: int, x: int, y: int, model: str) -> Tile:
    """One tile's heights, from the mosaics as they are now. Blocking (rasterio): run it in a
    thread. Raises TileError for what this machine cannot answer at all."""
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, not {model!r}")
    if not valid(z, x, y):
        raise ValueError(f"no tile {z}/{x}/{y} (zoom 0..{MAX_Z})")
    P = config.dem_paths()
    lons, lats = tile_grid(z, x, y)
    waiting: list[str] = []
    if z <= GLOBAL_MAX_Z:
        H, src = global_heights(P["global_dem"], lons, lats)
        n_ok = int(np.isfinite(H).sum())
        counts = {"ok": n_ok, "missing": N * N - n_ok}
    else:
        try:
            g = ground.sample_ground(lons, lats, ground.TERRAIN_BARE if model == "bare" else ground.TERRAIN_SURFACE)
        except ground.GroundConfigError as e:
            raise TileError(str(e)) from e
        H = np.asarray(g.height, float)
        src = np.array([SOURCES.get(s, 0) if st == "ok" else 0 for s, st in zip(g.src, g.status)], "u1")
        counts = g.counts()
        if z >= CUZK_MIN_Z and any(g.fetchable):
            waiting.append("cuzk")
        if model == "surface" and z >= GLO30_MIN_Z and any(g.glo30_missing):
            waiting.append("glo30")
        status = np.array(g.status)
        if model == "bare":
            # holes arrive filled from ground.sample_ground ("dtm10.fill"). The global model is for ground ČÚZK does not cover (or not yet): a hole inside its
            # built ground the fill cannot reach stays a hole, and the tile says "nodata"
            miss = ~np.isfinite(H) & (status != "nodata")
        else:
            # DSM10 sheets not fetched yet: drawn from GLO-30 there, each sample saying so, and
            # the tile waits for "cuzk" -- the ground query says not_fetched; a picture has to
            # show something
            wait = np.where(np.array(g.fetchable))[0]
            if len(wait):
                h3 = ground._sample_model(P["glo30_vrt"], lons[wait], lats[wait])
                k = wait[np.isfinite(h3)]
                H[k], src[k] = h3[np.isfinite(h3)], SOURCES["glo30.vrt"]
            miss = ~np.isfinite(H)
        if miss.any():
            Hg, sg = global_heights(P["global_dem"], lons[miss], lats[miss])
            H[miss], src[miss] = Hg, sg
    if z >= FINE_Z:
        fine_vrt = P["bare2_vrt" if model == "bare" else "dsm2_vrt"]
        try:
            h2 = ground._sample_model(fine_vrt, lons, lats)
        except ground.GroundConfigError as e:
            raise TileError(str(e)) from e
        m = np.isfinite(h2)
        H[m] = h2[m]
        src[m] = SOURCES.get(os.path.basename(fine_vrt), 0)
        if model == "bare":
            # the 2 m mosaic's own holes inside built ground, from its own ground around them
            gap = np.where(~m & np.isin(status, ("ok", "nodata")))[0]
            if len(gap):
                hf = ground.filled_heights(fine_vrt, lons[gap], lats[gap])
                k = gap[np.isfinite(hf)]
                H[k], src[k] = hf[np.isfinite(hf)], SOURCES["dtm2.fill"]
    try:
        Nh = datum.geoid_heights(lats, lons)
    except datum.GeoidMissing as e:
        raise TileError(f"no geoid grid to make the heights ellipsoidal: {e}") from e
    if not np.isfinite(H).all():
        waiting.append("nodata")
    return Tile(z, x, y, model, H + Nh, src, counts, waiting)


def source_stamp(model: str) -> dict[str, list]:
    """What a terrain release's tiles are made from, as (size, mtime) per mosaic: a release made
    on one stamp must not make tiles from another (store.Release.check_sources)."""
    P = config.dem_paths()
    keys = ["global_dem", "glo30_vrt", "bare_vrt" if model == "bare" else "dsm_vrt",
            "bare2_vrt" if model == "bare" else "dsm2_vrt", "geoid"]
    out: dict = {"generator": GENERATOR}
    for k in keys:
        try:
            st = os.stat(P[k])
            out[k] = [st.st_size, int(st.st_mtime)]
        except OSError:
            out[k] = None
    return out
