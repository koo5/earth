"""Every path and limit the service reads, from the environment at CALL time (tests point them at a
temp dir). One variable per file; nothing is derived from anything else.

The defaults are the layout of the terrain volume builder/ writes (`/dem`), mounted read-only.
"""
from __future__ import annotations

import os


def env(name: str, default: str) -> str:
    return os.getenv(name, default)


def dem_paths() -> dict[str, str]:
    return {
        # ČÚZK DMR 5G (bare earth) at 10 m, and the sheet index whose footprints say where one exists
        "bare_vrt": env("EARTH_BARE_VRT", "/dem/cuzk/dtm10.vrt"),
        "bare_index": env("EARTH_DTM_INDEX", "/dem/cuzk/dl/dmr5g/index.json"),
        # ČÚZK DMP 1G (surface) at 10 m, and its index
        "dsm_vrt": env("EARTH_DSM_VRT", "/dem/cuzk/dsm10.vrt"),
        "dsm_index": env("EARTH_DSM_INDEX", "/dem/cuzk/dl/dmp1g/index.json"),
        # the 2 m mosaics, read by the fine tiles (zoom >= tiles.FINE_Z) where built
        "bare2_vrt": env("EARTH_BARE2_VRT", "/dem/cuzk/dtm2.vrt"),
        "dsm2_vrt": env("EARTH_DSM2_VRT", "/dem/cuzk/dsm2.vrt"),
        # Copernicus GLO-30 (surface, the world), its mosaic and the directory of its 1-degree tiles
        "glo30_vrt": env("EARTH_GLO30_VRT", "/dem/glo30.vrt"),
        "glo30_tiles": env("EARTH_GLO30_TILES", "/dem/glo30"),
        # the whole earth, coarse: ETOPO 2022, 1 arc-minute, EGM2008 heights, the sea as bathymetry
        "global_dem": env("EARTH_GLOBAL_DEM", "/dem/global/ETOPO_2022_v1_60s_N90W180_surface.tif"),
        # EGM2008 geoid, for ellipsoidal heights
        "geoid": env("EARTH_GEOID_GRID", "/dem/geoid/us_nga_egm08_25.tif"),
    }


def egm96_grid() -> str:
    """EGM96 (PROJ's egm96_15.gtx): the city models' source heights are EGM96 (citymodels.py).
    Kept in the store, which earth alone writes."""
    return env("EARTH_EGM96_GRID", "/var/lib/earth/grids/egm96_15.gtx")


def store_dir() -> str:
    """Releases, channels and the tile packs (store.py)."""
    return env("EARTH_STORE", "/var/lib/earth")


def ground_max_points() -> int:
    """The most points one ground query may ask for (a 65 x 65 tile is 4,225)."""
    return int(env("EARTH_GROUND_MAX_POINTS", "20000"))


def limit(name: str, default: float) -> float:
    return float(env(f"EARTH_LIMIT_{name}", str(default)))


def no_limits() -> bool:
    return env("EARTH_NO_LIMITS", "") in ("1", "true", "yes")


def trusted_proxies() -> frozenset[str]:
    """The peers whose X-Forwarded-For is believed: the one proxy in front (Caddy). Anyone else's
    header is ignored and the peer address is the client."""
    return frozenset(p.strip() for p in env("EARTH_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip())
