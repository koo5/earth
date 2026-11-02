"""tiles.make and the wire formats, against the synthetic world (conftest.py). From hillview's
workbench's tile tests, without its fetch queue: here a tile that would
need data still to come says what it waits for and is not complete."""
import numpy as np

from earth import ground as gd, tiles
from conftest import A, GEOID_N, make_tif, make_vrt, plane, tile_of


def test_a_tile_inside_built_ground_is_ellipsoidal_and_complete(world):
    z, x, y = tile_of(50.01, 14.02, 15)
    t = tiles.make(z, x, y, "bare")
    assert t.complete and t.waiting == []
    lons, lats = tiles.tile_grid(z, x, y)
    ok = t.src == tiles.SOURCES["dtm10.vrt"]
    assert ok.mean() > 0.95                          # the one nodata cell of A may fall inside
    assert np.abs(t.heights[ok] - (plane(lons, lats)[ok] + GEOID_N)).max() < 0.01
    assert np.isfinite(t.heights).all()
    w, s_, e, n = tiles.tile_bounds(z, x, y)
    assert lons[0] == w and lats[0] == n             # the north-west corner first


def test_a_tile_over_unbuilt_sheets_waits_for_them(world):
    z, x, y = tile_of(50.01, 14.06, 15)               # in sheet B, not built
    t = tiles.make(z, x, y, "bare")
    assert t.waiting == ["cuzk"] and not t.complete
    # drawn meanwhile from the global model (200 m + N)
    assert np.allclose(t.heights, 200.0 + GEOID_N, atol=0.01)
    assert set(np.unique(t.src)) == {tiles.SOURCES["etopo60"]}
    # coarser than CUZK_MIN_Z, the same ground is final
    assert tiles.make(*tile_of(50.01, 14.04, 9), "bare").complete


def test_a_surface_tile_over_a_dsm_sheet_not_fetched_draws_glo30_and_waits(world):
    t = tiles.make(*tile_of(50.01, 14.06, 15), "surface")     # in sheet B, its DSM10 not built
    assert t.waiting == ["cuzk"] and not t.complete
    assert set(np.unique(t.src)) == {tiles.SOURCES["glo30.vrt"]}
    assert np.allclose(t.heights, 50.0 + GEOID_N, atol=0.01)


def test_a_surface_tile_past_the_glo30_on_disk_waits_from_zoom_10(world):
    assert tiles.make(*tile_of(51.5, 15.5, 9), "surface").complete
    assert tiles.make(*tile_of(51.5, 15.5, 10), "surface").waiting == ["glo30"]
    # a bare tile never waits for GLO-30: it is a surface model, never bare earth
    assert tiles.make(*tile_of(51.5, 15.5, 10), "bare").complete


def test_fine_tiles_read_the_2_m_mosaic(world):
    make_tif(str(world / "warp/dtm2/AAAA00.tif"), A, lambda lo, la: plane(lo, la) + 1.0)
    make_vrt(str(world / "dtm2.vrt"), [("warp/dtm2/AAAA00.tif", A)], A)
    z, x, y = tile_of(50.01, 14.02, 16)
    t = tiles.make(z, x, y, "bare")
    lons, lats = tiles.tile_grid(z, x, y)
    ok = t.src == tiles.SOURCES["dtm2.vrt"]
    assert ok.mean() > 0.95
    assert np.abs(t.heights[ok] - (plane(lons, lats)[ok] + 1.0 + GEOID_N)).max() < 0.01
    assert set(t.used()) <= {"dtm2.vrt", "dtm10.vrt", "etopo60"}
    t15 = tiles.make(*tile_of(50.01, 14.02, 15), "bare")
    assert "dtm2.vrt" not in t15.used()


def test_coarse_tiles_are_the_global_model_with_the_sea_at_sea_level(world):
    t = tiles.make(*tile_of(45.0, 10.0, 3), "surface")   # land to 30 E and 60 N, sea beyond
    land, sea = t.src == tiles.SOURCES["etopo60"], t.src == tiles.SOURCES["sea"]
    assert t.complete and land.any() and sea.any() and (land | sea).all()
    assert np.allclose(t.heights[sea], GEOID_N, atol=0.01)     # 0 m, not -4000
    assert tiles.make(0, 0, 0, "bare").complete


def test_q16_round_trips_within_5_cm_and_keeps_its_holes():
    rng = np.random.default_rng(1)
    h = (300 + np.cumsum(rng.normal(0, 3, 65 * 65))).astype("<f4")
    src = np.full(65 * 65, 2, "u1")
    h[100:140] = np.nan
    src[100:140] = 0
    body = tiles.encode_q16(h, src)
    assert body is not None and len(body) == 12 + 3 * 65 * 65
    got, s = tiles.decode_q16(body)
    ok = src > 0
    assert np.abs(got.ravel()[ok] - h[ok]).max() <= 0.051
    assert np.isnan(got.ravel()[~ok]).all() and (s.ravel() == src).all()
    h2 = np.zeros(65 * 65, "<f4")
    h2[1] = 4000.0
    assert tiles.encode_q16(h2, src) is None             # too steep for int16: the caller sends f32


def test_a_tile_says_what_it_is_made_from(world):
    s = tiles.source_stamp("bare")
    assert set(s) == {"generator", "global_dem", "glo30_vrt", "bare_vrt", "bare2_vrt", "geoid"}
    assert s["generator"] == tiles.GENERATOR         # a new way of making tiles is new tiles
    assert s["bare2_vrt"] is None and s["bare_vrt"] is not None


def test_a_hole_in_built_ground_is_the_ground_around_it(world):
    z, x, y = tile_of(50.0145, 14.0055, 15)          # over A's nodata cell
    t = tiles.make(z, x, y, "bare")
    lons, lats = tiles.tile_grid(z, x, y)
    filled = t.src == tiles.SOURCES["dtm10.fill"]
    assert filled.any() and t.complete
    assert np.abs(t.heights[filled] - (plane(lons, lats)[filled] + GEOID_N)).max() < 0.5
    # the global model (200 m here) only past A's western and northern edges, where no sheet is
    g = t.src == tiles.SOURCES["etopo60"]
    assert ((lons[g] < A[0]) | (lats[g] > A[3])).all()


def _dtm2_with_a_building(world, w, s, e, n):
    """the 2 m mosaic over sheet A, the plane + 1 m, with no ground inside (w, s, e, n): a roof"""
    def fn(lo, la):
        h = plane(lo, la) + 1.0
        return np.where((lo > w) & (lo < e) & (la > s) & (la < n), -9999.0, h + 0 * lo * la)
    make_tif(str(world / "warp/dtm2/AAAA00.tif"), A, fn)
    make_vrt(str(world / "dtm2.vrt"), [("warp/dtm2/AAAA00.tif", A)], A)


def test_ground_under_a_roof_is_the_ground_around_it_and_neighbours_meet(world):
    # a building 0.004 x 0.003 deg (~290 x 330 m) straddling the edge between two zoom-16 tiles
    z, x, y = tile_of(50.01, 14.02, 16)
    edge = (x + 1) / 2 ** z * 360 - 180
    _dtm2_with_a_building(world, edge - 0.002, 50.0085, edge + 0.002, 50.0115)
    a, b = tiles.make(z, x, y, "bare"), tiles.make(z, x + 1, y, "bare")
    for t in (a, b):
        lons, lats = tiles.tile_grid(t.z, t.x, t.y)
        roof = t.src == tiles.SOURCES["dtm2.fill"]
        assert roof.any() and "etopo60" not in t.used() and t.complete
        assert np.abs(t.heights[roof] - (plane(lons, lats)[roof] + 1.0 + GEOID_N)).max() < 1.0
    # a's east column is b's west column: the same samples, filled from the same ground
    ha, hb = a.heights.reshape(tiles.N, tiles.N), b.heights.reshape(tiles.N, tiles.N)
    assert (a.src.reshape(tiles.N, tiles.N)[:, -1] == tiles.SOURCES["dtm2.fill"]).any()
    assert np.allclose(ha[:, -1], hb[:, 0], rtol=0, atol=1e-6)


def test_a_hole_beyond_the_reach_stays_a_hole(world, monkeypatch):
    # a 6 x 6 cell lake in the 10 m mosaic, and a reach of two cells: its middle has no ground near
    make_tif(str(world / "warp/dtm10/AAAA00.tif"), A, plane, hole=(slice(4, 10), slice(10, 16)))
    monkeypatch.setattr(gd, "FILL_REACH_M", 100.0)
    t = tiles.make(*tile_of(50.0115, 14.013, 15), "bare")
    assert t.complete and t.waiting == [] and t.counts["nodata"] > 0   # final: the same mosaics, the same hole
    lost = ~np.isfinite(t.heights)
    assert lost.any() and (t.src[lost] == 0).all()   # no height, and no global model under it
    assert "etopo60" not in t.used()
