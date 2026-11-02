"""Background maintainer (the overview and the disk budget) and server launch.

The maintainer runs as a daemon thread in this (entrypoint) process; the server
runs as a child so we can forward termination signals for graceful shutdown.
"""
from __future__ import annotations

import signal
import subprocess
import threading
import time

import overview
import sheets as S
from build import build_lock
from store import Store

APP_DIR = "/app"


def maintain_once(cfg, store: Store, known, now: float | None = None) -> None:
    """One pass: the overview takes the waiting sheets when the hysteresis says so."""
    now = time.time() if now is None else now
    pending = store.pending_overview()
    if overview.due(pending, now, cfg.ov_min_sheets, cfg.ov_quiet_s, cfg.ov_max_age_s):
        with build_lock(cfg):
            ids = [sid for sid, _ in pending if store.jp2(sid).exists()]
            if ids:
                overview.update(cfg, [str(store.jp2(s)) for s in ids])
                dropped = overview.drop_cached(cfg.tile_cache, ids, S.detail_zoom(cfg.crossover))
                print(f"[overview] took {len(ids)} sheets; {dropped} cached low-zoom tiles dropped", flush=True)
            store.mark_overview([sid for sid, _ in pending])


def _maintain(cfg, known) -> None:
    store = Store.of(cfg)
    try:
        with build_lock(cfg):
            overview.create(cfg, known)          # once: low zoom is transparent until then
    except Exception as exc:  # noqa: BLE001 — say it, keep the thread for the next pass
        print(f"[overview] create failed: {exc}", flush=True)
    while True:
        try:
            maintain_once(cfg, store, known)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintain] pass failed, retried in {cfg.maintain_s:.0f} s: {exc}", flush=True)
        time.sleep(cfg.maintain_s)


def start_maintainer(cfg, known) -> None:
    threading.Thread(target=_maintain, args=(cfg, known), daemon=True).start()


def run_server(cfg) -> int:
    # gunicorn on wsgi.py, never MapProxy's own server: that one would render detail tiles past the sheet fetch
    # gthread: the timeout is the worker's heartbeat, not the request, so a tile that waits minutes for its
    # sheet no longer gets its worker killed at 120 s (the 502s of recon pl03)
    cmd = ["gunicorn", "-b", f"0.0.0.0:{cfg.port}", "-w", str(cfg.workers), "-k", "gthread",
           "--threads", str(cfg.threads), "--timeout", "120", "wsgi:application"]
    print(f"[start] serving on :{cfg.port}  "
          "XYZ: /wmts/oi/webmercator/{z}/{x}/{y}.png  viewer: /map",
          flush=True)
    print(f"[start] launching: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=APP_DIR)
    print(f"[start] server process started (pid={proc.pid})", flush=True)
    # Forward SIGTERM/SIGINT to the server child for a graceful shutdown.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, _f: proc.send_signal(s))
    code = proc.wait()
    print(f"[start] server process exited with code {code}", flush=True)
    return code
