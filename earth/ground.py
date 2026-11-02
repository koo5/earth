"""The ground at a point, of the kind the caller names: ČÚZK bare earth or surface, GLO-30.

(From hillview's workbench, 2026-10-13: the sampling half. builder/ builds the mosaics this
reads; the service reads them, read-only, and says which points are missing rather than fetching
anything itself.)
"""
from __future__ import annotations

import json
import math
import os

from . import config

# THE GROUND, ASKED FOR BY NAME (2026-09-29). This used to be one stack walked per point,
# "/dem/cuzk/dtm10.vrt,/dem/glo30.vrt", first finite wins -- so a point in a hole of the ČÚZK
# mosaic (an unbuilt sheet: Brandys and b101 both had them) silently came back from GLO-30, a
# SURFACE model that stands on canopy and roofs, 17 m off bare earth at a point where both
# exist. And a batch that straddled the seam was worse than either model alone: a fit through
# 24 glo30 points and 22 dtm10 points 7 m apart read a 3.13 m bow and a -807 mm/m tilt on
# spotA46, pure artefact of the seam.
#
# Now every caller says WHICH ground it means, and every point says what happened to it:
#
#   want="bare"     ČÚZK DTM10 (DMR 5G, bare earth) and nothing else. Never GLO-30.
#   want="surface"  ČÚZK DSM10 (DMP 1G) where it is built, else GLO-30; `src` names which, and
#                   `single_source=True` refuses a batch that would span both ("mixed").
#
#   status  ok           a height, from the model named in `src`; bare ground under a roof or on
#                        water, where lidar saw no ground, is filled from the bare ground round it
#                        and named "<mosaic>.fill" ("dtm10.fill"; filled_heights)
#           not_fetched  a ČÚZK sheet covers the point but is not in the mosaic yet -- the
#                        terrain worker's fetch_cuzk job builds it (POST /terrain/ground)
#           outside      no model of this kind exists there (no ČÚZK sheet; GLO-30 ocean)
#           nodata       the covering data IS built and still has no value at the point (bare:
#                        no ground within FILL_REACH_M of it either)
#           mixed        single_source=True and the batch would span two models
#
# The API mounts /dem read-only, so it never fetches anything itself; it only says what is
# missing, and terrain_fetch.request_fetch hands that to the worker.
TERRAIN_BARE = "bare"
TERRAIN_SURFACE = "surface"
GROUND_STATUSES = ("ok", "not_fetched", "outside", "nodata", "mixed")

# A point whose bilinear neighbourhood reaches into an unbuilt neighbour sheet is NaN even when
# its own sheet is built, so "is anything around it missing" looks this far round the point:
# 1.5 cells of the 10 m mosaic in latitude (~15 m), generous in longitude.
_NEIGHBOURHOOD_DEG = 0.0003
# How far a hole in built bare ground is filled from the ground around it. Measured 2026-10-16
# over 1236 built DMR 5G sheets at 10 m: 3.2 % of the cells are holes, half of them more
# than 50 m from ground, the farthest 480 m (ponds and rivers, the border). The point query
# answered a hole "nodata" until 2026-10-17 while the tiles drew it filled: a camera on the
# pavement beside a building in Brandýs had no bare ground. The workbench's copy does the same; a
# fix to either belongs in the other.
FILL_REACH_M = float(os.getenv("EARTH_FILL_REACH_M", "600"))
# points are filled in blocks this wide, each read with the reach round it: a batch spread over
# the country reads a window per block, never one window over all of it
_FILL_BLOCK_M = 5000.0


class GroundConfigError(RuntimeError):
    """The ground cannot be classified here: a configured index is missing or unreadable, or a
    retired setting is still in the environment. Raised, never papered over -- the caller turns
    it into a note or an HTTP error that names it."""


class SheetIndex:
    """The ČÚZK sheet footprints of one dataset, from download_cuzk.py's index.json, bucketed
    on a coarse grid so a lookup touches a handful of sheets instead of all 16301.

    THE FOOTPRINT IS THE FEED'S OWN RECTANGLE. The ATOM feed publishes each sheet as a
    georss:polygon, which download_cuzk reduces to a bbox -- and that loses nothing: checked
    2026-09-29 over all 16301 DMR 5G entries, every polygon is the same axis-aligned WGS84
    rectangle (5 points, 2 distinct lats, 2 distinct lons). The true sheet is an S-JTSK rectangle,
    slightly rotated in WGS84, so these rectangles overlap their neighbours a little and reach a
    little past the state border. Consequence: "outside" is never claimed for a point a sheet
    covers, and a point just across the border reads not_fetched (then nodata once built), not
    outside."""
    CELL_DEG = 0.05

    def __init__(self, sheets: list[dict]):
        import numpy as _np
        self.codes = [str(s["code"]) for s in sheets]
        self.bb = _np.array([s["bbox"] for s in sheets], float).reshape(-1, 4)
        self.grid: dict[tuple[int, int], list[int]] = {}
        c = self.CELL_DEG
        for k, (w, s_, e, n) in enumerate(self.bb):
            for i in range(math.floor(w / c), math.floor(e / c) + 1):
                for j in range(math.floor(s_ / c), math.floor(n / c) + 1):
                    self.grid.setdefault((i, j), []).append(k)

    def touching(self, w: float, s: float, e: float, n: float) -> list[int]:
        """Sheets whose footprint intersects the box (a point is a box of zero size)."""
        c = self.CELL_DEG
        cand = set()
        for i in range(math.floor(w / c), math.floor(e / c) + 1):
            for j in range(math.floor(s / c), math.floor(n / c) + 1):
                cand.update(self.grid.get((i, j), ()))
        bb = self.bb
        return sorted(k for k in cand
                      if not (bb[k, 2] < w or e < bb[k, 0] or bb[k, 3] < s or n < bb[k, 1]))


_INDEX_CACHE: dict[str, tuple[float, SheetIndex]] = {}
_VRT_CACHE: dict[str, tuple[float, frozenset]] = {}


def load_sheet_index(path: str) -> SheetIndex:
    """Loaded once per file version (keyed by mtime: the worker rewrites it atomically)."""
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        raise GroundConfigError(
            f"the ČÚZK sheet index {path} is missing -- the terrain worker writes it on its first "
            f"ČÚZK fetch (download_cuzk.py index --dataset <dmr5g|dmp1g> --out <its dl dir>); "
            f"without it no point can be told apart as outside or not yet fetched")
    hit = _INDEX_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path) as f:
            sheets = json.load(f)
        idx = SheetIndex(sheets)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise GroundConfigError(f"the ČÚZK sheet index {path} is unreadable: "
                                f"{type(e).__name__}: {e}")
    if not idx.codes:
        raise GroundConfigError(f"the ČÚZK sheet index {path} lists no sheets")
    _INDEX_CACHE[path] = (mt, idx)
    return idx


def vrt_members(path: str) -> frozenset:
    """The tiles a VRT mosaic actually references, by stem (a ČÚZK sheet code, a GLO-30 tile
    name). A tile on disk that the VRT does not list answers nothing, so it counts as not fetched
    -- the fetch job's VRT rebuild is what picks it up. Read from the VRT's XML, not from a
    directory listing. Empty when the VRT does not exist yet."""
    import xml.etree.ElementTree as ET
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return frozenset()
    hit = _VRT_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        raise GroundConfigError(f"the mosaic {path} is unreadable: {type(e).__name__}: {e}")
    names = frozenset(os.path.splitext(os.path.basename(e.text.strip()))[0]
                      for e in root.iter("SourceFilename") if e.text)
    _VRT_CACHE[path] = (mt, names)
    return names


def bilinear(src, lons, lats, block: int = 512):
    """Interpolated elevation, because `src.sample()` is NEAREST and that is a staircase here.

    glo30's cells are ~20 m and a panorama walk steps ~4.3 m, so nearest snaps several frames
    onto one cell: measured on the church walk it gives a per-step change of median 0.00 m and
    a MAX of 6.27 m at cell boundaries, where bilinear gives 0.18 and 1.26. A 6 m jump between
    consecutive frames is a 146 % grade that nothing on the ground did.

    It costs about 0.5 m of spurious terrain residual — nothing against a broken solve (17.20
    -> 17.59) and half the score of a good one (`church-first20-6x60-rig` 0.91 -> 0.50), so it
    matters exactly where the measurement has to be trusted.

    NaN, never an edge value, for a point the raster cannot interpolate: outside its extent, or
    in the half cell between the outermost cell centres and the edge. It used to clip the index
    into the window, so a point kilometres off the raster in a mixed batch came back as the
    nearest edge cell's height -- a plausible number for ground that is not there.

    Cell values sit at cell CENTRES (rowcol's fractional index is 0.5 at the first centre). It
    used to treat them as sitting on the top-left corners, which moved the whole model half a
    cell -- 5 m on dtm10, 15 m on glo30.

    Points are read in blocks of `block` x `block` cells, so a batch spread over the whole mosaic
    reads a few small windows instead of one window the size of the country.
    """
    import numpy as _np
    import rasterio
    import rasterio.transform
    import rasterio.windows

    lons = _np.atleast_1d(_np.asarray(lons, float))
    lats = _np.atleast_1d(_np.asarray(lats, float))
    out = _np.full(len(lons), _np.nan)
    if not len(lons):
        return out
    rows, cols = rasterio.transform.rowcol(src.transform, lons, lats, op=float)
    # continuous index where integer k is the CENTRE of cell k
    r = _np.atleast_1d(_np.asarray(rows, float)) - 0.5
    c = _np.atleast_1d(_np.asarray(cols, float)) - 0.5
    inside = (_np.isfinite(r) & _np.isfinite(c)
              & (r >= 0) & (r <= src.height - 1) & (c >= 0) & (c <= src.width - 1))
    if not inside.any():
        return out
    idx = _np.where(inside)[0]
    keys = (_np.floor(r[idx] / block).astype(int), _np.floor(c[idx] / block).astype(int))
    for key in set(zip(*keys)):
        sel = idx[(keys[0] == key[0]) & (keys[1] == key[1])]
        rr, cc = r[sel], c[sel]
        r0 = int(_np.floor(rr.min())); c0 = int(_np.floor(cc.min()))
        r1 = min(int(_np.floor(rr.max())) + 2, src.height)
        c1 = min(int(_np.floor(cc.max())) + 2, src.width)
        # rasterio 1.5.0 ships an incomplete stub: pyright reads Window as taking NO
        # parameters, so positional and keyword both "fail". Verified against the installed
        # package -- inspect.signature gives (col_off, row_off, width, height), which is the
        # order below, and both forms construct.
        _win = rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)  # type: ignore[call-arg]
        A = src.read(1, window=_win).astype(float)
        if src.nodata is not None:
            A = _np.where(A == src.nodata, _np.nan, A)
        lr, lc = rr - r0, cc - c0
        # a point exactly on the last centre row/column has no +1 neighbour; its weight is 0
        i0 = _np.minimum(_np.floor(lr).astype(int), A.shape[0] - 1)
        j0 = _np.minimum(_np.floor(lc).astype(int), A.shape[1] - 1)
        i1 = _np.minimum(i0 + 1, A.shape[0] - 1)
        j1 = _np.minimum(j0 + 1, A.shape[1] - 1)
        fr, fc = lr - i0, lc - j0
        out[sel] = (A[i0, j0] * (1 - fr) * (1 - fc) + A[i1, j0] * fr * (1 - fc)
                    + A[i0, j1] * (1 - fr) * fc + A[i1, j1] * fr * fc)
    return out


def _sample_model(path: str, lons, lats):
    """One model's heights, NaN where it has none. A missing file answers nothing (the caller
    classifies why); a file that exists and cannot be read raises GroundConfigError -- a broken
    raster is not an absence of ground."""
    import numpy as _np
    import rasterio
    from rasterio.errors import RasterioError

    got = _np.full(len(lons), _np.nan)
    if not len(lons) or not os.path.exists(path):
        return got
    try:
        with rasterio.open(path) as src:
            nd = src.nodata
            got = bilinear(src, lons, lats)
    except RasterioError as e:
        raise GroundConfigError(f"the mosaic {path} is unreadable: {type(e).__name__}: {e}")
    bad = ~_np.isfinite(got) | (got <= -1000)
    if nd is not None:
        bad |= (got == nd)
    got[bad] = _np.nan
    return got


def filled_heights(path: str, lons, lats, reach_m: float | None = None):
    """Bare ground at the points, the mosaic's holes filled from the ground around them (GDAL
    fillnodata: inverse distance from the nearest ground in every direction, no smoothing); NaN
    where no ground lies within `reach_m`. Each window is a block's points plus the reach, on the
    mosaic's own grid, so a cell is filled from exactly the same ground whichever query or tile
    asks: two neighbours meet without a step."""
    import numpy as np

    reach_m = FILL_REACH_M if reach_m is None else reach_m
    lons = np.asarray(lons, float)
    lats = np.asarray(lats, float)
    out = np.full(len(lons), np.nan)
    if not len(lons) or not os.path.exists(path):
        return out
    m_lon = 111_320.0 * math.cos(math.radians(float(np.mean(lats))))
    blocks: dict[tuple[int, int], list[int]] = {}
    for i, (lo, la) in enumerate(zip(lons, lats)):
        blocks.setdefault((int(lo * m_lon // _FILL_BLOCK_M), int(la * 111_320.0 // _FILL_BLOCK_M)), []).append(i)
    import rasterio

    # opened once: the country's dtm10.vrt lists 16,301 sheets, and opening it costs ~0.5 s -- once per block,
    # it was 34 of a cold z9 tile's 84 s on earth.hillview.cz (2026-11-02, ea08)
    with rasterio.open(path) as ds:
        for idx in blocks.values():
            out[idx] = _filled_window(ds, lons[idx], lats[idx], reach_m)
    return out


def _filled_window(ds, lons, lats, reach_m: float):
    import numpy as np
    import rasterio.transform
    from rasterio.fill import fillnodata
    from rasterio.io import MemoryFile
    from rasterio.windows import Window

    out = np.full(len(lons), np.nan)
    nd = ds.nodata if ds.nodata is not None else -9999.0
    m_lon = 111_320.0 * math.cos(math.radians(float(np.mean(lats))))
    cell_m = min(abs(ds.res[0]) * m_lon, abs(ds.res[1]) * 111_320.0)
    reach = int(math.ceil(reach_m / cell_m))
    rows, cols = rasterio.transform.rowcol(ds.transform, lons, lats, op=float)
    r0, r1 = int(math.floor(np.min(rows))) - reach - 2, int(math.ceil(np.max(rows))) + reach + 2
    c0, c1 = int(math.floor(np.min(cols))) - reach - 2, int(math.ceil(np.max(cols))) + reach + 2
    win = Window(c0, r0, c1 - c0, r1 - r0)
    # the part on the raster, read as it is; the rest nodata. Not rasterio's boundless read: that goes through
    # a WarpedVRT, and was most of the other half of those 84 s
    a = np.full((r1 - r0, c1 - c0), nd, "float32")
    rr0, rr1, cc0, cc1 = max(r0, 0), min(r1, ds.height), max(c0, 0), min(c1, ds.width)
    if rr0 < rr1 and cc0 < cc1:
        a[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = ds.read(1, window=Window(cc0, rr0, cc1 - cc0, rr1 - rr0))
    transform = ds.window_transform(win)
    crs = ds.crs
    ground_ = np.isfinite(a) & (a != nd) & (a > -1000)
    if not ground_.any():
        return out
    a[~ground_] = nd
    a = fillnodata(a, mask=ground_.astype("uint8"), max_search_distance=reach, smoothing_iterations=0)
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="float32",
                     crs=crs, transform=transform, nodata=nd) as mem:
            mem.write(a, 1)
            h = np.asarray(bilinear(mem, lons, lats), float)
    h[~np.isfinite(h) | (h == nd) | (h <= -1000)] = np.nan
    return h


def _glo30_tile(lon: float, lat: float) -> str:
    """download_glo30.tile_name for the 1-degree tile holding the point (that module lives in
    the terrain worker image, so the name is spelled here)."""
    tlat, tlon = math.floor(lat), math.floor(lon)
    return (f"Copernicus_DSM_COG_10_{'N' if tlat >= 0 else 'S'}{abs(tlat):02d}_00_"
            f"{'E' if tlon >= 0 else 'W'}{abs(tlon):03d}_00_DEM")


class Ground:
    """What sample_ground found, per point, and the models it used."""

    def __init__(self, want: str, points: list[tuple[float, float]]):
        import numpy as _np
        n = len(points)
        self.want = want
        self.points = points
        self.height = _np.full(n, _np.nan)
        self.src: list[str | None] = [None] * n
        self.status: list[str | None] = [None] * n
        # points a ČÚZK fetch (of `fetch_layer`) would change: every not_fetched point that
        # lies in a sheet which is not built yet
        self.fetchable: list[bool] = [False] * n
        self.fetch_layer = "dtm" if want == TERRAIN_BARE else "dsm"
        # surface points whose GLO-30 tile is not on the volume: a different fetch (the
        # terrain worker's glo30 layer), asked for by whoever needs the far ground
        self.glo30_missing: list[bool] = [False] * n

    @property
    def ok(self):
        import numpy as _np
        return _np.array([s == "ok" for s in self.status], bool)

    def counts(self) -> dict:
        out = {k: 0 for k in GROUND_STATUSES}
        for i, s in enumerate(self.status):
            if s is None:
                raise RuntimeError(f"ground point {i} of {len(self.status)} was never "
                                   f"classified; every point must get one of {GROUND_STATUSES}")
            out[s] += 1
        return out

    def sources(self) -> dict:
        """model -> the number of points it answered (status ok, or mixed: a refused batch still
        says which models it would have spanned)."""
        out: dict[str, int] = {}
        for s, st in zip(self.src, self.status):
            if st in ("ok", "mixed"):
                if s is None:
                    raise RuntimeError(f"a point is {st} but names no model: "
                                       f"a status was set without its source")
                out[s] = out.get(s, 0) + 1
        return out

    def summary(self) -> dict:
        """The JSON-ready tally every caller reports beside its number."""
        n = len(self.status)
        c = self.counts()
        return {"want": self.want, "n": n, "status": {k: v for k, v in c.items() if v},
                "sources": self.sources(),
                "ok_frac": round(c["ok"] / n, 3) if n else None}

    def fetch_points(self) -> list[tuple[float, float]]:
        return [p for p, f in zip(self.points, self.fetchable) if f]

    def glo30_points(self) -> list[tuple[float, float]]:
        return [p for p, f in zip(self.points, self.glo30_missing) if f]


def grid(bbox, step_m: float, max_points: int) -> list[list[float]]:
    """Points every step_m over [w, s, e, n], row by row from the south-west corner. (The
    workbench's routers/terrain.py ground_grid.) ValueError for a bad box or too many points."""
    import math
    w, s, e, n = bbox
    if not (w < e and s < n):
        raise ValueError(f"bbox must be [w, s, e, n] with w < e and s < n, got {list(bbox)}")
    if step_m <= 0:
        raise ValueError("step_m must be > 0")
    dlat = step_m / 110_540.0
    dlon = step_m / (111_320.0 * max(0.1, math.cos(math.radians((s + n) / 2))))
    nx, ny = int((e - w) / dlon) + 1, int((n - s) / dlat) + 1
    if nx * ny > max_points:
        raise ValueError(f"bbox at step_m={step_m:g} is {nx * ny} points, past {max_points}; "
                         f"raise step_m or shrink the bbox")
    return [[round(w + i * dlon, 7), round(s + j * dlat, 7)] for j in range(ny) for i in range(nx)]


def sample_ground(lons, lats, want: str, *, single_source: bool = False) -> Ground:
    """The ground at each (lon, lat), of the kind the caller NAMES. See the block above.

    want="bare"     ČÚZK DTM10 only; `src` is its file name on every ok point, or "dtm10.fill"
                    where a hole in it is filled from the ground round it (filled_heights).
    want="surface"  ČÚZK DSM10 where built, else GLO-30; `src` says which, per point. Ground
                    under a ČÚZK sheet that is not fetched yet is not_fetched, never GLO-30.
    single_source   (surface) refuse a batch that would span both: every point that had a height
                    becomes status "mixed" with NaN, `src` still naming the model that answered
                    it, so the caller can see the split. A bare batch has one source by
                    construction.

    Raises GroundConfigError when the sheet index it needs is missing: without it an absent
    height cannot be told apart as "outside" or "not fetched yet", and guessing is how this
    function used to go wrong.
    """
    import numpy as _np

    if want not in (TERRAIN_BARE, TERRAIN_SURFACE):
        raise ValueError(f"want must be {TERRAIN_BARE!r} or {TERRAIN_SURFACE!r}, not {want!r}")
    P = config.dem_paths()
    lons = _np.atleast_1d(_np.asarray(lons, float))
    lats = _np.atleast_1d(_np.asarray(lats, float))
    if lons.shape != lats.shape:
        raise ValueError(f"{len(lons)} lons against {len(lats)} lats")
    g = Ground(want, list(zip(lons.tolist(), lats.tolist())))
    if not len(lons):
        return g

    cuzk_vrt = P["bare_vrt"] if want == TERRAIN_BARE else P["dsm_vrt"]
    index = load_sheet_index(P["bare_index"] if want == TERRAIN_BARE else P["dsm_index"])
    built = vrt_members(cuzk_vrt)
    name = os.path.basename(cuzk_vrt)
    h = _sample_model(cuzk_vrt, lons, lats)
    for i in _np.where(_np.isfinite(h))[0]:
        g.height[i], g.src[i], g.status[i] = h[i], name, "ok"

    def _cuzk_missing(lon, lat) -> tuple[bool, bool]:
        """(inside a sheet footprint, a sheet at or next to the point is not built)"""
        inside = bool(index.touching(lon, lat, lon, lat))
        near = index.touching(lon - _NEIGHBOURHOOD_DEG, lat - _NEIGHBOURHOOD_DEG,
                              lon + _NEIGHBOURHOOD_DEG, lat + _NEIGHBOURHOOD_DEG)
        return inside, any(index.codes[k] not in built for k in near)

    if want == TERRAIN_BARE:
        for i in range(len(lons)):
            if g.status[i] == "ok":
                continue
            inside, missing = _cuzk_missing(lons[i], lats[i])
            if not inside:
                g.status[i] = "outside"
            elif missing:
                g.status[i], g.fetchable[i] = "not_fetched", True
            else:
                g.status[i] = "nodata"
        hole = _np.array([i for i, st in enumerate(g.status) if st == "nodata"], int)
        if len(hole):
            hf = filled_heights(cuzk_vrt, lons[hole], lats[hole])
            fname = os.path.splitext(name)[0] + ".fill"
            for k, i in enumerate(hole):
                if _np.isfinite(hf[k]):
                    g.height[i], g.src[i], g.status[i] = hf[k], fname, "ok"
        return g

    # surface: GLO-30 answers where ČÚZK DSM10 did not -- but never for ground whose DSM10 sheet
    # is simply not fetched yet. It did until 2026-10-19: a town whose four sheets nobody had
    # fetched read as surface "ok" from GLO-30 at every frame, so nothing ever asked for them.
    # Such a point is not_fetched; GLO-30 is for ground outside ČÚZK's sheets and for the holes
    # inside built ones (src names it either way). The workbench's copy does the same.
    for i in range(len(lons)):
        if g.status[i] is None:
            inside, missing = _cuzk_missing(lons[i], lats[i])
            if inside and missing:
                g.status[i], g.fetchable[i] = "not_fetched", True
    miss = _np.array([s is None for s in g.status], bool)
    if miss.any():
        gname = os.path.basename(P["glo30_vrt"])
        idx = _np.where(miss)[0]
        h2 = _sample_model(P["glo30_vrt"], lons[idx], lats[idx])
        for k, i in enumerate(idx):
            if _np.isfinite(h2[k]):
                g.height[i], g.src[i], g.status[i] = h2[k], gname, "ok"
    g30 = vrt_members(P["glo30_vrt"])
    for i in range(len(lons)):
        if g.status[i] is not None:     # ok, or a ČÚZK sheet not fetched (above)
            continue
        tile = _glo30_tile(lons[i], lats[i])
        if tile in g30:
            g.status[i] = "nodata"
        elif os.path.exists(os.path.join(P["glo30_tiles"], tile + ".ocean")):
            # the worker's own record that the bucket has no tile here: sea
            g.status[i] = "outside"
        else:
            # GLO-30 is global, so a missing tile is one nothing has fetched yet: not `fetchable`
            # (that is the ČÚZK fetch), but `glo30_missing` -- the worker's glo30 layer gets it
            g.status[i] = "not_fetched"
            g.glo30_missing[i] = True
    if single_source and len(g.sources()) > 1:
        for i in range(len(lons)):
            if g.status[i] == "ok":
                g.status[i], g.height[i] = "mixed", _np.nan
    return g
