"""Heights above the ellipsoid, from heights above the geoid.

Every DEM the service reads is ORTHOMETRIC (height above the geoid: ČÚZK in Bpv, GLO-30 and ETOPO
in EGM2008, sub-metre apart). Tiles are served ELLIPSOIDAL, h = H + N, with N the EGM2008 geoid
height, so a client places every sample by pure geometry and needs no geoid model of its own.

(From hillview's workbench, 2026-10-13: only the transform.)
"""
from __future__ import annotations

import os

import numpy as np

from . import config

_transformers: dict[str, object] = {}


class GeoidMissing(FileNotFoundError):
    pass


def _to_orthometric():
    """h (ellipsoid) -> H (geoid) through the EGM2008 grid. vgridshift ADDS the grid going forward,
    so multiplier=-1 is H = h - N. The grid is named explicitly rather than via PROJ_DATA."""
    grid = config.dem_paths()["geoid"]
    t = _transformers.get(grid)
    if t is None:
        if not os.path.isfile(grid):
            raise GeoidMissing(f"geoid grid {grid} is not on this machine "
                               f"(https://cdn.proj.org/us_nga_egm08_25.tif)")
        from pyproj import Transformer
        t = Transformer.from_pipeline(
            "+proj=pipeline +step +proj=axisswap +order=2,1 +step +proj=unitconvert +xy_in=deg "
            f"+xy_out=rad +step +proj=vgridshift +grids={grid} +multiplier=-1 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg +step +proj=axisswap +order=2,1")
        _transformers[grid] = t
    return t


def geoid_heights(lats, lons) -> np.ndarray:
    """N(lat, lon), metres: how far the geoid sits above the WGS84 ellipsoid at each point."""
    lats = np.asarray(lats, float)
    _, _, neg = _to_orthometric().transform(lats, np.asarray(lons, float), np.zeros_like(lats))
    return -np.asarray(neg, float)
