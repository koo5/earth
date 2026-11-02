#!/usr/bin/env python3
"""Container entrypoint: check the zoom split, reconcile the held sheets, write config, serve.

Sheets arrive on demand (wsgi.py), so nothing is built before serving: the tile index is the record of what
is on disk, and the overview is created (empty, once) and filled by the background maintainer.
"""
import sys

import sheets as S
from config import Config, write_mapfile, write_mapproxy
from serve import run_server, start_maintainer
from store import Store


def main() -> int:
    cfg = Config.from_env()
    known = S.load()
    zoom = S.detail_zoom(cfg.crossover)          # refuses a crossover that splits a zoom level
    print(f"[start] config: data={cfg.data_dir} state={cfg.state_dir} cache={cfg.cache_dir} "
          f"port={cfg.port} workers={cfg.workers} sheets={len(known)} from {cfg.sheets_url}; "
          f"detail (sheets fetched) from zoom {zoom}; budget {cfg.budget_bytes / 1e9:.0f} GB, "
          f"{cfg.min_free_bytes / 1e9:.0f} GB kept free; "
          f"overview after {cfg.ov_min_sheets} sheets + {cfg.ov_quiet_s:.0f} s quiet, "
          f"or {cfg.ov_max_age_s:.0f} s", flush=True)
    cfg.ensure_dirs()
    Store.of(cfg).reconcile(known)
    write_mapfile(cfg, known)
    write_mapproxy(cfg)
    start_maintainer(cfg, known)
    return run_server(cfg)


if __name__ == "__main__":
    sys.exit(main())
