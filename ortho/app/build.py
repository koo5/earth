"""Running GDAL's long builds with progress heartbeats, and the build lock.

(The detail VRT and the whole-overview rebuild
are gone: sheets arrive on demand, store.py indexes them and overview.py fills
the overview in place.)
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
from pathlib import Path

BUILD_LOG_INTERVAL = max(1.0, float(os.environ.get("BUILD_LOG_INTERVAL", "15")))


def _output_status(path: str) -> str:
    """Return a compact status string for a GDAL temporary output."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return "temp output not created yet"
    return f"temp output {size:,} bytes"


def _run_logged(cmd: list[str], label: str, *, output_path: str | None = None) -> None:
    """Run a long GDAL command and emit periodic progress heartbeats.

    GDAL's command-line tools do not provide useful progress for these metadata
    and raster-building operations when stdout is redirected. The heartbeat
    makes a slow filesystem scan distinguishable from a dead container.
    """
    started = time.monotonic()
    print(f"[build] {label} started", flush=True)
    # niced: the overview is background work, and the tile renders it ran beside right after a batch of sheets
    # took 80 s once (recon pl04)
    proc = subprocess.Popen(["nice", "-n", "15", *cmd], stdout=subprocess.DEVNULL)
    next_report = started + BUILD_LOG_INTERVAL
    try:
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            now = time.monotonic()
            if now >= next_report:
                detail = f"; {_output_status(output_path)}" if output_path else ""
                print(f"[build] {label} still running ({now - started:.0f}s{detail})",
                      flush=True)
                next_report = now + BUILD_LOG_INTERVAL
            time.sleep(min(1.0, max(0.05, next_report - time.monotonic())))
    except BaseException:
        proc.terminate()
        proc.wait()
        raise

    elapsed = time.monotonic() - started
    if returncode:
        print(f"[build] {label} failed after {elapsed:.1f}s (exit {returncode})", flush=True)
        raise subprocess.CalledProcessError(returncode, cmd)
    detail = f"; {_output_status(output_path)}" if output_path else ""
    print(f"[build] {label} finished in {elapsed:.1f}s{detail}", flush=True)


@contextlib.contextmanager
def build_lock(cfg):
    """Cross-process exclusive lock: one overview update at a time."""
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    fh = open(f"{cfg.state_dir}/.build.lock", "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
