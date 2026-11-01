"""Which ČÚZK orthophoto sheets a Web Mercator tile needs.

A sheet id is its lower-left corner in ETRS89/TM33 kilometres: 602_5554 covers E 602000-604000,
N 5554000-5556000, 16000 x 16000 pixels at 0.125 m (its .j2w says so). So the sheets under a tile are
arithmetic on the tile's TM33 box; sheets.txt (make_sheets.py, from ČÚZK's Atom feed) says which exist.
"""
from __future__ import annotations

import math
from pathlib import Path

from pyproj import Transformer

SHEET_M = 2000
LIST = Path(__file__).with_name("sheets.txt")
HALF = 20037508.342789244                      # Web Mercator's half width, metres
# Czechia's latitude span, for the detail-zoom check: every sheet lies inside it
LAT_SPAN = (48.5, 51.1)
# MapServer's scale denominators: the OGC 0.28 mm pixel, or its 72 dpi default (0.3528 mm); the split
# zoom is accepted only if it holds under both
PIXEL_M = (0.00028, 0.0254 / 72)

_to_tm33 = Transformer.from_crs("EPSG:3857", "EPSG:25833", always_xy=True)


def load(path: Path = LIST) -> dict[str, str]:
    """{sheet id: ČÚZK's last update} for every sheet that exists."""
    known = {}
    for line in path.read_text().splitlines():
        sid, updated = line.split()
        known[sid] = updated
    return known


def sheet_box(sid: str) -> tuple[int, int, int, int]:
    e, n = (int(v) * 1000 for v in sid.split("_"))
    return e, n, e + SHEET_M, n + SHEET_M


def extent(known) -> tuple[int, int, int, int]:
    """The TM33 box round every sheet that exists."""
    boxes = [sheet_box(s) for s in known]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def tile_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2 ** z
    minx, maxy = -HALF + x * size, HALF - y * size
    return minx, maxy - size, minx + size, maxy


def tile_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """An XYZ tile's box in TM33 (EPSG:25833, the sheets' grid), edges densified."""
    return _to_tm33.transform_bounds(*tile_3857(z, x, y), densify_pts=21)


def ids_in_box(box, known, margin: float = 2.0) -> list[str]:
    """The existing sheets a TM33 box touches, with `margin` metres to spare (resampling reads a pixel or
    two past a tile's edge: a neighbour that is missing would show as a seam)."""
    minx, miny, maxx, maxy = box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin
    ids = []
    for e in range(math.floor(minx / SHEET_M), math.floor(maxx / SHEET_M) + 1):
        for n in range(math.floor(miny / SHEET_M), math.floor(maxy / SHEET_M) + 1):
            sid = f"{e * 2}_{n * 2}"
            if sid in known:
                ids.append(sid)
    return ids


def ids_for_tile(z: int, x: int, y: int, known) -> list[str]:
    return ids_in_box(tile_box(z, x, y), known)


def scale_denominators(z: int) -> list[float]:
    """MapServer's scale denominator for a 256 px tile at zoom z, at both ends of Czechia's latitudes and
    under both pixel conventions."""
    width = 2 * HALF / 2 ** z
    return [width * math.cos(math.radians(lat)) / (256 * px) for lat in LAT_SPAN for px in PIXEL_M]


def detail_zoom(crossover: float) -> int:
    """The first zoom MapServer draws from the sheets (scale below `crossover`) at every Czech latitude
    under either pixel convention, with the zoom above it drawn from the overview everywhere. Only then
    can a tile's zoom alone say whether it needs sheets; a crossover that splits a zoom is refused."""
    for z in range(1, 23):
        here, above = scale_denominators(z), scale_denominators(z - 1)
        if max(here) < crossover:
            if min(above) <= crossover:
                raise ValueError(f"CROSSOVER {crossover} splits zoom {z - 1} (scale {min(above):.0f}-"
                                 f"{max(above):.0f} across Czechia): pick one between zoom levels")
            return z
    raise ValueError(f"CROSSOVER {crossover} is below every zoom's scale")
