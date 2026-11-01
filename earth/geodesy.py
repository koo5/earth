"""Where a point on the earth IS, in one world frame: WGS84 geodetic -> ECEF -> east/north/up.

THE WORLD WAS FLAT (until 2026-10-01). Every solve was carried into the world by the flat-earth
linearisation -- metres per degree at the origin's latitude, heights untouched -- which is right
next to the origin and wrong with distance: the ground falls away from a tangent plane by d^2/2R,
1.3 m at the town 4 km from the world origin, 785 m at 100 km. A terrain tile placed exactly and
a solve placed flat would part by that much, and the telephoto overlay this is for looks 100 km.

So the world frame is the local tangent frame (ENU) at an ORIGIN on the ellipsoid, and anything
is placed by its true position: lat, lon and ELLIPSOIDAL height through ECEF. A solve is a rigid
body, small next to the earth, so carrying it is one rotation and one translation -- from the
tangent frame at its own centre to the world's -- and nothing inside it is deformed.

HEIGHTS. The bench's heights are orthometric (vertical_datum.py), the geometry needs ellipsoidal
ones: h = H + N, the geoid height N from EGM2008. A run's frame sits at (its centre, h = N_c) so
its up = 0 is the geoid there; the world's at (origin, h = N_0), so world up near the origin is an
orthometric height -- the numbers stay the ones the DEM and the readouts speak.

The browser has the same arithmetic (hillview_common world/geodesy.ts) and is pinned to this one
by a shared fixture, world/geodesy-cases.json, copied here as tests/geodesy-cases.json and checked
to match; it never needs N, because the service serves tile heights already ellipsoidal.

(From hillview's workbench, 2026-10-13.)
"""
from __future__ import annotations

import math

import numpy as np

A = 6378137.0                    # WGS84 semi-major axis, m
F = 1 / 298.257223563            # flattening
E2 = F * (2 - F)                 # first eccentricity squared


def to_ecef(lat, lon, h):
    """Geodetic degrees and ellipsoidal metres -> ECEF metres. Arrays or scalars."""
    la, lo = np.radians(np.asarray(lat, float)), np.radians(np.asarray(lon, float))
    h = np.asarray(h, float)
    s, c = np.sin(la), np.cos(la)
    n = A / np.sqrt(1 - E2 * s * s)
    return np.stack([(n + h) * c * np.cos(lo), (n + h) * c * np.sin(lo), (n * (1 - E2) + h) * s], -1)


def from_ecef(xyz):
    """ECEF -> (lat, lon, h). Bowring's start and two refinements: well under a millimetre."""
    xyz = np.asarray(xyz, float)
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - E2))
    for _ in range(4):
        s = np.sin(lat)
        n = A / np.sqrt(1 - E2 * s * s)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - E2 * n / (n + h)))
    s = np.sin(lat)
    n = A / np.sqrt(1 - E2 * s * s)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def enu_basis(lat: float, lon: float) -> np.ndarray:
    """Rows: east, north, up, in ECEF, at a geodetic point."""
    la, lo = math.radians(lat), math.radians(lon)
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    return np.array([[-so, co, 0.0],
                     [-sl * co, -sl * so, cl],
                     [cl * co, cl * so, sl]])


class Frame:
    """The tangent frame at (lat, lon, h): ENU metres, h ellipsoidal."""

    def __init__(self, lat: float, lon: float, h: float = 0.0):
        self.lat, self.lon, self.h = float(lat), float(lon), float(h)
        self.R = enu_basis(self.lat, self.lon)                 # ECEF -> ENU (rows)
        self.o = to_ecef(self.lat, self.lon, self.h)

    def from_geodetic(self, lat, lon, h):
        return (to_ecef(lat, lon, h) - self.o) @ self.R.T

    def to_geodetic(self, enu):
        return from_ecef(np.asarray(enu, float) @ self.R + self.o)


def rigid(local: Frame, world: Frame) -> np.ndarray:
    """4x4 that carries `local` ENU coordinates into `world` ENU: p_w = M[:3,:3] p + M[:3,3]."""
    Rot = world.R @ local.R.T
    t = world.R @ (local.o - world.o)
    M = np.eye(4)
    M[:3, :3] = Rot
    M[:3, 3] = t
    return M
