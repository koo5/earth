"""Shared constants + helpers for the integration tests."""
import math
import os
import pathlib
import re
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = os.environ.get("CONTAINER_CLI", "docker")
IMAGE = "cuzk-oi:test"
CONTAINER = "cuzk-oi-test"
PORT = 18080                       # distinct from the compose stack on 8080
BASE = f"http://127.0.0.1:{PORT}"

# Tile ids encode the SW corner in km, e.g. 302_5564 -> E 302000-304000, N 5564000-5566000.
_TILE_RE = re.compile(r"(\d+)_(\d+)\.jp2$")


def find_fixture_tile():
    """The real sheet the container suite serves: the largest <id>.jp2 in CUZK_FIXTURE_DIR (full imagery =>
    an opaque centre, for the JPEG test), with its .j2w beside it. None without the variable."""
    if not os.environ.get("CUZK_FIXTURE_DIR"):
        return None
    d = pathlib.Path(os.environ["CUZK_FIXTURE_DIR"])
    cands = [p for p in d.glob("*.jp2") if _TILE_RE.search(p.name)]
    if not cands:
        raise SystemExit(f"CUZK_FIXTURE_DIR={d} holds no <E>_<N>.jp2 sheet")
    return max(cands, key=lambda p: p.stat().st_size)


def tile_center_wgs84(jp2):
    """Centre of a tile (from its id) in WGS84 (lat, lon), via EPSG:25833 -> 4326."""
    m = _TILE_RE.search(jp2.name)
    e = int(m.group(1)) * 1000 + 1000      # +1 km -> centre of the 2 km tile
    n = int(m.group(2)) * 1000 + 1000
    from pyproj import Transformer
    lon, lat = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True).transform(e, n)
    return lat, lon


FIXTURE_TILE = find_fixture_tile()
TILE_CENTER = tile_center_wgs84(FIXTURE_TILE) if FIXTURE_TILE else (None, None)


def http(path, timeout=30):
    """GET BASE+path -> (status, content_type, body). HTTP errors return their code."""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def deg2tile(lat, lon, z):
    """WGS84 -> slippy XYZ tile (x, y) at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y
