"""ground.sample_ground against the synthetic world (conftest.py). From hillview's workbench."""
import os

import numpy as np
import pytest
import rasterio

from earth import ground as gd
from conftest import A, HOLE, CELL, make_tif, plane

P_OK = (14.0123, 50.0071)
P_NOT_FETCHED = (14.06, 50.01)               # in B, which is not built
P_OUTSIDE = (15.5, 51.5)                     # no sheet; no GLO-30 tile either
P_EDGE = (A[2] - 0.0002, 50.01)              # inside A, but its last half cell touches B
P_HOLE = (A[0] + (HOLE[1] + 0.5) * CELL, A[3] - (HOLE[0] + 0.5) * CELL)
P_SEA = (16.5, 51.5)                         # a GLO-30 .ocean marker
P_G30 = (14.15, 50.05)                       # no ČÚZK sheet; on the GLO-30 tile


def _ask(pts, want, **kw):
    return gd.sample_ground([p[0] for p in pts], [p[1] for p in pts], want, **kw)


def test_bare_classifies_every_point(world, monkeypatch):
    # a lake 9 x 9 cells (~600 x 1000 m) round the hole: its middle has no ground within 200 m
    make_tif(str(world / "warp/dtm10/AAAA00.tif"), A, plane, hole=(slice(1, 10), slice(1, 10)))
    monkeypatch.setattr(gd, "FILL_REACH_M", 200.0)
    g = _ask([P_OK, P_NOT_FETCHED, P_OUTSIDE, P_EDGE, P_HOLE], "bare")
    assert g.status == ["ok", "not_fetched", "outside", "not_fetched", "nodata"]
    assert g.height[0] == pytest.approx(plane(*P_OK), abs=1e-3)    # cell centres, not corners
    assert g.src[0] == "dtm10.vrt"
    assert np.isnan(g.height[1:]).all()
    assert g.fetchable == [False, True, False, True, False]
    assert g.fetch_points() == [P_NOT_FETCHED, P_EDGE]
    assert g.summary()["status"] == {"ok": 1, "not_fetched": 2, "outside": 1, "nodata": 1}


def test_a_hole_in_built_bare_ground_is_the_ground_round_it_and_says_so(world):
    """Lidar sees a roof, not the ground under it (Brandýs, 2026-10-17: a camera on the pavement
    beside one had no bare ground). The tiles drew it filled; the point query now answers the same
    ground, named dtm10.fill -- never GLO-30."""
    g = _ask([P_HOLE, P_OK], "bare")
    assert g.status == ["ok", "ok"] and g.src == ["dtm10.fill", "dtm10.vrt"]
    assert g.height[0] == pytest.approx(plane(*P_HOLE), abs=0.5)


def test_holes_far_apart_are_filled_in_blocks_and_each_as_alone(world, monkeypatch):
    pts = [P_HOLE, (P_HOLE[0] + 0.0001, P_HOLE[1] - 0.0001), P_OK]
    vrt = str(world / "dtm10.vrt")
    whole = gd.filled_heights(vrt, [p[0] for p in pts], [p[1] for p in pts])
    monkeypatch.setattr(gd, "_FILL_BLOCK_M", 100.0)
    blocks = gd.filled_heights(vrt, [p[0] for p in pts], [p[1] for p in pts])
    alone = [gd.filled_heights(vrt, [p[0]], [p[1]])[0] for p in pts]
    assert np.isfinite(whole).all()
    assert np.allclose(whole, blocks, atol=1e-4) and np.allclose(whole, alone, atol=1e-4)


def _boundless_fill(vrt, lons, lats, reach_m):
    """The fill as it read until 2026-11-02 (ea08): rasterio's boundless window, the reference for the clipped one."""
    import math
    import rasterio.transform
    from rasterio.fill import fillnodata
    from rasterio.io import MemoryFile
    from rasterio.windows import Window
    with rasterio.open(vrt) as ds:
        nd = ds.nodata if ds.nodata is not None else -9999.0
        m_lon = 111_320.0 * math.cos(math.radians(float(np.mean(lats))))
        reach = int(math.ceil(reach_m / min(abs(ds.res[0]) * m_lon, abs(ds.res[1]) * 111_320.0)))
        rows, cols = rasterio.transform.rowcol(ds.transform, lons, lats, op=float)
        r0, r1 = int(math.floor(np.min(rows))) - reach - 2, int(math.ceil(np.max(rows))) + reach + 2
        c0, c1 = int(math.floor(np.min(cols))) - reach - 2, int(math.ceil(np.max(cols))) + reach + 2
        win = Window(c0, r0, c1 - c0, r1 - r0)
        a = ds.read(1, window=win, boundless=True, fill_value=nd).astype("float32")
        transform, crs = ds.window_transform(win), ds.crs
    ok = np.isfinite(a) & (a != nd) & (a > -1000)
    a[~ok] = nd
    a = fillnodata(a, mask=ok.astype("uint8"), max_search_distance=reach, smoothing_iterations=0)
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="float32",
                     crs=crs, transform=transform, nodata=nd) as mem:
            mem.write(a, 1)
            h = np.asarray(gd.bilinear(mem, lons, lats), float)
    h[~np.isfinite(h) | (h == nd) | (h <= -1000)] = np.nan
    return h


def test_the_fill_reads_the_mosaic_once_and_answers_as_the_boundless_read_did(world, monkeypatch):
    """A cold z9 bare tile on the built country took 84 s, 34 of them reopening dtm10.vrt per 5 km block and most
    of the rest rasterio's boundless reads (ea08). Every window here runs off the raster's edge."""
    make_tif(str(world / "warp/dtm10/AAAA00.tif"), A, plane, hole=(slice(1, 6), slice(30, 38)))
    vrt = str(world / "dtm10.vrt")
    pts = [P_HOLE, (A[0] + 0.0335, A[3] - 0.0025), (A[0] + 0.0005, A[1] + 0.0005), P_OK]
    lons, lats = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    opened = []
    real_open = rasterio.open
    monkeypatch.setattr(rasterio, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    monkeypatch.setattr(gd, "_FILL_BLOCK_M", 100.0)                 # a block per point
    got = gd.filled_heights(vrt, lons, lats, reach_m=300.0)
    assert opened.count(vrt) == 1
    monkeypatch.setattr(rasterio, "open", real_open)
    want = np.array([_boundless_fill(vrt, lons[i:i + 1], lats[i:i + 1], 300.0)[0] for i in range(len(pts))])
    assert np.isfinite(got).all() and np.array_equal(got, want)


def test_bare_never_answers_from_glo30(world):
    g = _ask([P_NOT_FETCHED], "bare")
    assert g.status == ["not_fetched"] and g.sources() == {}


def test_surface_names_its_model_and_single_source_refuses_a_mix(world):
    g = _ask([P_OK, P_G30, P_OUTSIDE, P_SEA], "surface")
    assert g.status == ["ok", "ok", "not_fetched", "outside"]
    assert g.src[:2] == ["dsm10.vrt", "glo30.vrt"]
    assert g.height[0] == pytest.approx(plane(*P_OK) + 10.0, abs=1e-3)
    assert g.height[1] == pytest.approx(50.0)
    assert g.fetchable == [False, False, False, False]
    assert g.glo30_missing == [False, False, True, False]
    m = _ask([P_OK, P_G30], "surface", single_source=True)
    assert m.status == ["mixed", "mixed"] and np.isnan(m.height).all()


def test_surface_under_a_sheet_not_fetched_is_not_fetched_never_glo30(world):
    """B's DSM10 sheet is not built; GLO-30 has 50 m there. A town whose sheets nobody had fetched
    read surface "ok" from GLO-30 at every frame, and so nothing ever asked for them (2026-10-19)."""
    g = _ask([P_NOT_FETCHED, P_EDGE, P_G30], "surface")
    assert g.status == ["not_fetched", "not_fetched", "ok"]
    assert np.isnan(g.height[:2]).all() and g.src[:2] == [None, None]
    assert g.fetchable == [True, True, False]


def test_unbuilt_vrt_means_not_fetched(world):
    os.remove(world / "dtm10.vrt")
    assert _ask([P_OK, P_OUTSIDE], "bare").status == ["not_fetched", "outside"]


def test_missing_index_and_broken_mosaic_are_loud(world):
    (world / "dtm10.vrt").write_text("<VRTDataset")
    with pytest.raises(gd.GroundConfigError, match="unreadable"):
        _ask([P_OK], "bare")
    os.remove(world / "dl/dmr5g/index.json")
    with pytest.raises(gd.GroundConfigError, match="index"):
        _ask([P_OK], "bare")


def test_want_is_checked(world):
    with pytest.raises(ValueError):
        _ask([P_OK], "dsm")


def test_bilinear_is_nan_outside_the_raster_even_in_a_mixed_batch(world):
    with rasterio.open(world / "warp/dtm10/AAAA00.tif") as src:
        got = gd.bilinear(src, [P_OK[0], 20.0, A[0] + 0.0002, A[0] + 0.0005], [P_OK[1], 55.0, 50.01, 50.0105])
    assert got[0] == pytest.approx(plane(*P_OK), abs=1e-3)
    assert np.isnan(got[1]) and np.isnan(got[2])
    assert got[3] == pytest.approx(plane(A[0] + 0.0005, 50.0105), abs=1e-3)
