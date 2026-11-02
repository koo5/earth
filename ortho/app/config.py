"""Runtime configuration (from env) and config-file generation.

Single source of truth for the MapServer mapfile and the MapProxy config —
the mapfile from a stdlib string template, the MapProxy YAML built as a dict and
dumped with PyYAML (no fragile heredoc escaping).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

import sheets as S

APP_DIR = "/app"
# what the entrypoint generates at each start (the mapfile, mapproxy.yaml): a tmpfs, so the image can run with a
# read-only root (deploy/compose.yml, recon pl03). /etc/mapserver.conf names it, baked in the Dockerfile.
GEN_DIR = "/run/ortho"
TEMPLATES = Path(__file__).parent / "templates"
MAPSERV_BIN = "/usr/lib/cgi-bin/mapserv"
# zooms 18-20 are drawn from the sheets each time and never stored: stored, they would outweigh the sheets
# (~350 MB of tiles per 75 MB sheet); zooms 15-17 are ~5 MB a sheet. wsgi.py sends them to this layer.
DIRECT_FROM_ZOOM = 18
DIRECT_LAYER = "oi_direct"


@dataclass
class Config:
    data_dir: str
    state_dir: str
    cache_dir: str
    lock_dir: str
    tile_lock_dir: str
    port: int
    workers: int
    srs: str
    ov_res: int
    crossover: int
    # on demand
    sheets_url: str
    fetch_jobs: int
    budget_bytes: int
    min_free_bytes: int
    ov_min_sheets: int
    ov_quiet_s: float
    ov_max_age_s: float
    ov_threads: int
    maintain_s: float

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=os.environ.get("DATA_DIR", "/data"),
            state_dir=os.environ.get("STATE_DIR", "/state"),
            cache_dir=os.environ.get("CACHE_DIR", "/cache"),
            lock_dir=os.environ.get("LOCK_DIR", "/tmp/mapproxy/locks"),
            tile_lock_dir=os.environ.get("TILE_LOCK_DIR", "/tmp/mapproxy/tile_locks"),
            port=int(os.environ.get("PORT", "8080")),
            workers=int(os.environ.get("WORKERS", "4")),
            srs=os.environ.get("SRS", "EPSG:25833"),
            ov_res=int(os.environ.get("OV_RES", "4")),
            crossover=int(os.environ.get("CROSSOVER", "16000")),
            # ČÚZK's open-data sheets, <id>.zip; a file:// directory works too (the tests)
            sheets_url=os.environ.get("SHEETS_URL", "https://openzu.cuzk.gov.cz/opendata/OI"),
            fetch_jobs=int(os.environ.get("FETCH_JOBS", "4")),
            # ~450 GB is ~6,000 sheets, ~30% of Czechia (a 1 TB server's share); past it nothing is fetched
            budget_bytes=int(float(os.environ.get("SHEET_BUDGET_GB", "450")) * 1e9),
            # and nothing either once the disk's free space is below this (the disk is shared)
            min_free_bytes=int(float(os.environ.get("MIN_FREE_GB", "50")) * 1e9),
            ov_min_sheets=int(os.environ.get("OV_MIN_SHEETS", "8")),
            ov_quiet_s=float(os.environ.get("OV_QUIET_S", "300")),
            ov_max_age_s=float(os.environ.get("OV_MAX_AGE_S", "3600")),
            ov_threads=int(os.environ.get("OV_THREADS", "2")),
            maintain_s=float(os.environ.get("MAINTAIN_S", "30")),
        )

    # derived paths
    @property
    def overview(self) -> str: return f"{self.state_dir}/overview.tif"
    @property
    def index(self) -> str: return f"{self.state_dir}/sheets.csv"
    @property
    def tile_cache(self) -> str: return f"{self.cache_dir}/oi"
    @property
    def mapfile(self) -> str: return f"{GEN_DIR}/oi.map"
    @property
    def mapproxy_yaml(self) -> str: return f"{GEN_DIR}/mapproxy.yaml"

    def ensure_dirs(self) -> None:
        for d in (GEN_DIR, self.cache_dir, self.state_dir, self.lock_dir, self.tile_lock_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def write_mapfile(cfg: Config, known) -> None:
    minx, miny, maxx, maxy = S.extent(known)        # every sheet that exists, held or not
    print(f"[start] extent ({cfg.srs}): {minx} {miny} {maxx} {maxy}", flush=True)
    tmpl = Template((TEMPLATES / "oi.map.tmpl").read_text())
    Path(cfg.mapfile).write_text(tmpl.substitute(
        srs=cfg.srs, srs_lower=cfg.srs.lower(),
        minx=minx, miny=miny, maxx=maxx, maxy=maxy,
        crossover=cfg.crossover, ov_res=cfg.ov_res,
        index=cfg.index, overview=cfg.overview,
    ))


def write_mapproxy(cfg: Config) -> None:
    # WMTS only, and the app takes only its REST GetTile (wsgi.py): every detail tile must pass the sheet
    # fetch first, so no other route may render one (WMS, the demo, TMS and KVP GetTile would draw it with
    # sheets missing and cache that for good)
    tiles = {
        "format": "mixed",              # JPEG for opaque tiles, PNG where transparent
        "request_format": "image/png",  # request alpha so mixed can decide per tile
        "grids": ["webmercator"],
        "sources": ["oi_src"],
        # tile by tile: a metatile would make one first view fetch every sheet under 4 x 4 tiles
        "meta_size": [1, 1],
        "meta_buffer": 0,
    }
    doc = {
        "services": {"wmts": {"kvp": False, "restful": True}},
        "layers": [{"name": "oi", "title": "Ortofoto CR 0.125m", "sources": ["oi_cache"]},
                   {"name": DIRECT_LAYER, "title": "Ortofoto CR 0.125m, zooms 18+", "sources": ["oi_direct"]}],
        "caches": {
            "oi_cache": {
                **tiles,
                # a known layout, so the overview can drop exactly the tiles a new sheet changes
                "cache": {"type": "file", "directory": cfg.tile_cache, "directory_layout": "tms"},
            },
            # the same tiles, drawn every time and never stored (DIRECT_FROM_ZOOM)
            "oi_direct": {**tiles, "disable_storage": True},
        },
        "sources": {
            "oi_src": {
                "type": "mapserver",
                "supported_srs": [cfg.srs],
                "req": {"map": cfg.mapfile, "layers": "oi_ov,oi", "transparent": True},
                "mapserver": {"binary": MAPSERV_BIN},
            },
        },
        "grids": {"webmercator": {"base": "GLOBAL_WEBMERCATOR", "num_levels": 21}},
        "globals": {
            "image": {"paletted": False},  # true-colour tiles (orthophoto)
            "cache": {
                "base_dir": cfg.cache_dir,
                # locks OUTSIDE the cache so a cache-clear can't delete them mid-render
                "lock_dir": cfg.lock_dir,
                "tile_lock_dir": cfg.tile_lock_dir,
            },
        },
    }
    with open(cfg.mapproxy_yaml, "w") as fh:
        yaml.safe_dump(doc, fh, default_flow_style=False, sort_keys=False)
