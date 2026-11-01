"""A tiny synthetic world in a temp dir: two ČÚZK sheets side by side in the index, only the western
one built; a GLO-30 tile under both; the whole earth coarse; a 45 m geoid. Every path comes from
the environment, as in production. (From hillview's workbench.)
"""
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

# the terrain builder's scripts (builder/) import each other by name, as they did in the workbench's
# enrich/terrain; their tests (tests/builder/) import them the same way
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builder"))

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

CELL = 0.001
A = [14.00, 50.00, 14.04, 50.02]          # built
B = [14.04, 50.00, 14.08, 50.02]          # in the index, not built
HOLE = (5, 5)                              # a nodata cell inside A
GEOID_N = 45.0


def plane(lon, lat):
    return 100.0 + 1000.0 * (lon - 14.0) + 500.0 * (lat - 50.0)


def make_tif(path, bbox, fn, hole=None):
    w, s, e, n = bbox
    nx, ny = round((e - w) / CELL), round((n - s) / CELL)
    lon = w + (np.arange(nx) + 0.5) * CELL
    lat = n - (np.arange(ny) + 0.5) * CELL
    data = fn(lon[None, :], lat[:, None]).astype("float32")
    if hole:
        data[hole] = -9999
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=nx, height=ny, count=1, dtype="float32",
                       crs="EPSG:4326", nodata=-9999, transform=from_origin(w, n, CELL, CELL)) as d:
        d.write(data, 1)


def make_vrt(path, tifs, bbox):
    """A hand-written VRT (no gdalbuildvrt needed) over `tifs`, each [(rel_path, bbox)]."""
    w, s, e, n = bbox
    nx, ny = round((e - w) / CELL), round((n - s) / CELL)
    src = ""
    for rel, (tw, ts, te, tn) in tifs:
        sx, sy = round((te - tw) / CELL), round((tn - ts) / CELL)
        ox, oy = round((tw - w) / CELL), round((n - tn) / CELL)
        src += (f'<ComplexSource><SourceFilename relativeToVRT="1">{rel}</SourceFilename>'
                f'<SourceBand>1</SourceBand><SrcRect xOff="0" yOff="0" xSize="{sx}" ySize="{sy}"/>'
                f'<DstRect xOff="{ox}" yOff="{oy}" xSize="{sx}" ySize="{sy}"/>'
                f'<NODATA>-9999</NODATA></ComplexSource>')
    with open(path, "w") as f:
        f.write(f'<VRTDataset rasterXSize="{nx}" rasterYSize="{ny}"><SRS>EPSG:4326</SRS>'
                f'<GeoTransform>{w}, {CELL}, 0, {n}, 0, {-CELL}</GeoTransform>'
                f'<VRTRasterBand dataType="Float32" band="1"><NoDataValue>-9999</NoDataValue>'
                f'{src}</VRTRasterBand></VRTDataset>')


def tile_of(lat, lon, z):
    k = 2 ** z
    return z, int((lon + 180) / 360 * k), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k)


@pytest.fixture
def world(tmp_path, monkeypatch):
    d = tmp_path / "dem"
    index = [{"code": "AAAA00", "feed_url": "x", "bbox": A, "zip_url": None, "zip_length": None},
             {"code": "BBBB00", "feed_url": "x", "bbox": B, "zip_url": None, "zip_length": None}]
    for ds in ("dmr5g", "dmp1g"):
        os.makedirs(d / "dl" / ds)
        (d / "dl" / ds / "index.json").write_text(json.dumps(index))
    make_tif(str(d / "warp/dtm10/AAAA00.tif"), A, plane, hole=HOLE)
    make_vrt(str(d / "dtm10.vrt"), [("warp/dtm10/AAAA00.tif", A)], A)
    make_tif(str(d / "warp/dsm10/AAAA00.tif"), A, lambda lo, la: plane(lo, la) + 10.0)
    make_vrt(str(d / "dsm10.vrt"), [("warp/dsm10/AAAA00.tif", A)], A)
    g30 = [14.0, 50.0, 14.2, 50.1]
    name = "Copernicus_DSM_COG_10_N50_00_E014_00_DEM"
    make_tif(str(d / "glo30" / f"{name}.tif"), g30, lambda lo, la: 0 * lo + 0 * la + 50.0)
    make_vrt(str(d / "glo30.vrt"), [(f"glo30/{name}.tif", g30)], g30)
    (d / "glo30" / "Copernicus_DSM_COG_10_N51_00_E016_00_DEM.ocean").write_text("")
    # the whole earth, coarse: 1-degree cells, land at 200 m over 0..30 E, 40..60 N, sea elsewhere,
    # 4 km deep (a sampler that takes <= -1000 m for nodata loses it: it did)
    gl = np.full((180, 360), -4000.0, "float32")
    gl[90 - 60:90 - 40, 180 + 0:180 + 30] = 200.0
    with rasterio.open(str(d / "global.tif"), "w", driver="GTiff", width=360, height=180, count=1,
                       dtype="float32", crs="EPSG:4326", transform=from_origin(-180, 90, 1, 1)) as dst:
        dst.write(gl, 1)
    (d / "geoid.tif").write_text("a stand-in: the transform is replaced below")
    env = {"EARTH_BARE_VRT": d / "dtm10.vrt", "EARTH_DTM_INDEX": d / "dl/dmr5g/index.json",
           "EARTH_DSM_VRT": d / "dsm10.vrt", "EARTH_DSM_INDEX": d / "dl/dmp1g/index.json",
           "EARTH_GLO30_VRT": d / "glo30.vrt", "EARTH_GLO30_TILES": d / "glo30",
           "EARTH_BARE2_VRT": d / "dtm2.vrt", "EARTH_DSM2_VRT": d / "dsm2.vrt",
           "EARTH_GLOBAL_DEM": d / "global.tif", "EARTH_GEOID_GRID": d / "geoid.tif",
           "EARTH_STORE": tmp_path / "store"}
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))

    from earth import datum

    class Geoid:                                   # vgridshift -1: returns H = h - N
        def transform(self, lat, lon, h):
            return lat, lon, np.asarray(h, float) - GEOID_N
    monkeypatch.setattr(datum, "_to_orthometric", lambda: Geoid())
    return d
