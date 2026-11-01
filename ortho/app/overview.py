"""The low-zoom overview: one 4 m mosaic over all of Czechia, filled in as sheets arrive.

Created once, empty and sparse, over the extent of every sheet that exists. A batch of newly fetched sheets
is warped INTO it in place and only the batch's extents of its pyramid are refreshed (gdaladdo
--partial-refresh-from-source-extent, GDAL >= 3.8); then the cached tiles of the overview zooms over those
extents are dropped, since they were drawn without the new sheets. An evicted sheet keeps its overview pixels.

When: never per sheet. due() is a hysteresis on the queue of sheets the overview lacks: an update runs once
at least OV_MIN_SHEETS wait AND none arrived for OV_QUIET_S (a burst of browsing settles first), or once
the oldest has waited OV_MAX_AGE_S (a trickle still gets in).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import sheets as S
from build import _run_logged

LEVELS = ["2", "4", "8", "16", "32", "64"]


def due(pending: list[tuple[str, float]], now: float, min_sheets: int, quiet_s: float, max_age_s: float) -> bool:
    """pending: (sheet id, arrived) the overview lacks, oldest first."""
    if not pending:
        return False
    oldest, newest = pending[0][1], max(t for _, t in pending)
    return (len(pending) >= min_sheets and now - newest >= quiet_s) or now - oldest >= max_age_s


def create(cfg, known) -> None:
    """The empty overview over every sheet's extent: sparse (unwritten blocks cost nothing and read as
    transparent), RGBA, its pyramid in place so later refreshes are partial. Once; written then renamed."""
    if Path(cfg.overview).exists():
        return
    minx, miny, maxx, maxy = S.extent(known)
    w, h = (maxx - minx) // cfg.ov_res, (maxy - miny) // cfg.ov_res
    tmp = f"{cfg.overview}.tmp"
    print(f"[overview] creating {w} x {h} at {cfg.ov_res} m over {minx},{miny} - {maxx},{maxy}", flush=True)
    _run_logged(["gdal_create", "-of", "GTiff", "-outsize", str(w), str(h), "-bands", "4", "-ot", "Byte",
                 "-a_srs", cfg.srs, "-a_ullr", str(minx), str(maxy), str(maxx), str(miny),
                 "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2", "-co", "BIGTIFF=YES",
                 "-co", "SPARSE_OK=TRUE", "-co", "ALPHA=YES", tmp], "overview create", output_path=tmp)
    _run_logged(["gdaladdo", "-r", "average", tmp, *LEVELS], "overview pyramid (empty)", output_path=tmp)
    os.replace(tmp, cfg.overview)


def update(cfg, jp2s: list[str]) -> None:
    """Warp these sheets into the overview, then refresh its pyramid over their extents only."""
    # every sheet opaque, white included: the alpha band says only where sheets are
    _run_logged(["gdalwarp", "-q", "-r", "average", "-dstalpha", "-multi", "-wo", f"NUM_THREADS={cfg.ov_threads}",
                 *jp2s, cfg.overview], f"overview warp ({len(jp2s)} sheets)")
    _run_logged(["gdaladdo", "-r", "average", "--partial-refresh-from-source-extent", ",".join(jp2s),
                 cfg.overview], f"overview pyramid refresh ({len(jp2s)} sheets)")


def cached_tiles(cache_dir: str, sids: list[str], below_zoom: int) -> list[Path]:
    """The cache files of zooms < below_zoom that overlap these sheets: MapProxy's file cache with the
    'tms' layout (config.write_mapproxy), {cache_dir}/{z}/{x}/{y}.{jpeg|png}, y counted from the south."""
    from pyproj import Transformer
    to_3857 = Transformer.from_crs("EPSG:25833", "EPSG:3857", always_xy=True)
    out = []
    for sid in sids:
        minx, miny, maxx, maxy = to_3857.transform_bounds(*S.sheet_box(sid), densify_pts=21)
        for z in range(below_zoom):
            size = 2 * S.HALF / 2 ** z
            x0, x1 = int((minx + S.HALF) // size), int((maxx + S.HALF) // size)
            ys0, ys1 = int((miny + S.HALF) // size), int((maxy + S.HALF) // size)   # tms rows: from the south
            for x in range(x0, x1 + 1):
                for y in range(ys0, ys1 + 1):
                    for ext in ("jpeg", "png"):
                        out.append(Path(cache_dir) / str(z) / str(x) / f"{y}.{ext}")
    return out


def drop_cached(cache_dir: str, sids: list[str], below_zoom: int) -> int:
    n = 0
    for p in set(cached_tiles(cache_dir, sids, below_zoom)):
        try:
            p.unlink()
            n += 1
        except FileNotFoundError:
            pass
    return n
