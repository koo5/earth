"""The sheets held on disk: fetched when a detail tile first needs them, indexed for MapServer, and kept for
good. A sheet is never downloaded twice: when the budget is reached (or the disk's free space runs low),
fetching stops and a tile that needs a sheet not held is refused; more storage is the answer, not eviction.

- A sheet is downloaded once, by whichever request asks first (a lock per sheet; several gunicorn workers),
  from ČÚZK's open-data endpoint as <id>.zip, and published as get_tiles.sh did: sidecars first, the .jp2
  last by an atomic rename, so a reader never sees half a sheet.
- MapServer finds the held sheets through a tile index, sheets.csv (location + its exact 2 km square as WKT,
  read by OGR's CSV driver), rewritten atomically on every change. Nothing reads a JP2 to index it.
- sheets.sqlite records each sheet's size, arrival and whether the overview has it yet; how long recent
  downloads took (the Retry-After a waiting tile is given), and the sheets whose download failed lately.
- A tile never waits for its sheets on the request (begin): their downloads start in the background and the
  tile is answered "come back in N s" at once (recon pl04: a request that held a worker for minutes held
  every cached tile behind it, and gunicorn killed it at 120 s).
"""
from __future__ import annotations

import contextlib
import fcntl
import math
import os
import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sheets as S


class SheetMissing(RuntimeError):
    pass


class StoreFull(RuntimeError):
    """The budget is reached or the disk is nearly full: no sheet is fetched any more."""


# a sheet whose download failed answers that failure for this long before it is tried again (in every worker:
# the failure is recorded in sheets.sqlite), so a sheet ČÚZK does not have is a 502, not a 503 for ever
FAIL_HOLD_S = 300
# Retry-After for a waiting tile: the median of recent downloads, times the queue ahead of it here
RETRY_MIN_S, RETRY_MAX_S, RETRY_FIRST_S = 5, 120, 20


class Store:
    def __init__(self, data_dir: str, state_dir: str, sheets_url: str, fetch_jobs: int,
                 budget_bytes: int, min_free_bytes: int):
        self.data, self.state = Path(data_dir), Path(state_dir)
        self.url = sheets_url.rstrip("/")
        self.jobs = fetch_jobs
        self.budget, self.min_free = budget_bytes, min_free_bytes
        self.db_path = self.state / "sheets.sqlite"
        self.index = self.state / "sheets.csv"
        for d in (self.data / ".locks", self.data / ".tmp", self.state):
            d.mkdir(parents=True, exist_ok=True)
        with self._db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS sheets (id TEXT PRIMARY KEY, bytes INTEGER NOT NULL, "
                        "arrived REAL NOT NULL, in_overview INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS downloads (id TEXT NOT NULL, seconds REAL NOT NULL, at REAL NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS failures (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                        "detail TEXT NOT NULL, at REAL NOT NULL)")
        self._queued: set[str] = set()                  # this process's background downloads, queued or running
        self._queued_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    @classmethod
    def of(cls, cfg) -> "Store":
        return cls(cfg.data_dir, cfg.state_dir, cfg.sheets_url, cfg.fetch_jobs, cfg.budget_bytes, cfg.min_free_bytes)

    # -- plumbing ---------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextlib.contextmanager
    def lock(self, name: str):
        with open(self.data / ".locks" / f"{name}.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    def jp2(self, sid: str) -> Path:
        return self.data / f"{sid}.jp2"

    def _files(self, sid: str) -> list[Path]:
        return [self.data / f"{sid}{ext}" for ext in (".jp2", ".j2w", ".gml")]

    # -- fetching ---------------------------------------------------------------------------------
    def ensure(self, ids: list[str]) -> int:
        """Every sheet in `ids` on disk and indexed, the missing ones fetched first; a sheet once here stays.
        How many this call fetched. SheetMissing when one cannot be fetched, StoreFull when fetching has
        stopped: a detail tile is never drawn with a sheet missing."""
        missing = [s for s in sorted(set(ids)) if not self.jp2(s).exists()]
        return len(self.fetch(missing)) if missing else 0

    def begin(self, ids: list[str]) -> tuple[int, int] | None:
        """None when every sheet in `ids` is on disk. Otherwise the missing ones are downloading in the
        background, started here unless one already is (in this worker or another, by its lock), and the
        answer is (how many this call started, seconds to come back). SheetMissing when one failed in the
        last FAIL_HOLD_S, StoreFull when fetching has stopped: never a tile drawn with a sheet missing."""
        missing = [s for s in sorted(set(ids)) if not self.jp2(s).exists()]
        if not missing:
            return None
        with self._db() as con:
            q = ",".join("?" * len(missing))
            failed = con.execute(f"SELECT id, kind, detail FROM failures WHERE id IN ({q}) AND at > ?",
                                 (*missing, time.time() - FAIL_HOLD_S)).fetchone()
        if failed:
            raise (StoreFull if failed[1] == "full" else SheetMissing)(failed[2])
        if self.full():
            raise StoreFull(f"sheet storage full ({self.held_bytes() / 1e9:.0f} GB held, budget "
                            f"{self.budget / 1e9:.0f} GB): {', '.join(missing)} not fetched")
        started = 0
        for sid in missing:
            with self._queued_lock:
                if sid in self._queued or self._in_flight(sid):
                    continue
                self._queued.add(sid)
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max(1, self.jobs), thread_name_prefix="sheets")
            self._pool.submit(self._background, sid)
            started += 1
        return started, self.retry_after()

    def retry_after(self) -> int:
        """Seconds a waiting tile is told to come back in: the median of the last downloads, times the
        queue in this worker over its parallel downloads."""
        with self._db() as con:
            recent = [r[0] for r in con.execute("SELECT seconds FROM downloads ORDER BY at DESC LIMIT 9")]
        each = statistics.median(recent) if recent else RETRY_FIRST_S
        rounds = math.ceil(max(1, len(self._queued)) / max(1, self.jobs))
        return int(min(RETRY_MAX_S, max(RETRY_MIN_S, math.ceil(each * rounds))))

    def _in_flight(self, sid: str) -> bool:
        """Whether another worker holds the sheet's lock: it is downloading it."""
        with open(self.data / ".locks" / f"{sid}.lock", "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False

    def _background(self, sid: str) -> None:
        t = time.time()
        try:
            if self.ensure([sid]):
                with self._db() as con:
                    con.execute("INSERT INTO downloads (id, seconds, at) VALUES (?, ?, ?)", (sid, time.time() - t, time.time()))
                    con.execute("DELETE FROM failures WHERE id = ?", (sid,))
        except Exception as e:  # noqa: BLE001 -- recorded for the next ask of it, in any worker
            kind = "full" if isinstance(e, StoreFull) else "missing"
            with self._db() as con:
                con.execute("INSERT OR REPLACE INTO failures (id, kind, detail, at) VALUES (?, ?, ?, ?)",
                            (sid, kind, str(e)[:500], time.time()))
            print(f"[sheets] {sid} failed: {e}", flush=True)
        finally:
            with self._queued_lock:
                self._queued.discard(sid)

    def full(self) -> bool:
        """At the budget, or the disk's free space below the reserve (it is shared with the rest of earth)."""
        return self.held_bytes() >= self.budget or shutil.disk_usage(self.data).free < self.min_free

    def fetch(self, ids: list[str]) -> list[str]:
        """Fetch the sheets in `ids` not on disk yet, several at once; the ones this call fetched. The ones
        that arrived are indexed even when another failed; then the first failure is raised."""
        with ThreadPoolExecutor(max(1, min(self.jobs, len(ids)))) as pool:
            futures = [pool.submit(self._fetch, s) for s in ids]
        new, failed = [], None
        for s, f in zip(ids, futures):
            if f.exception() is not None:
                failed = failed or f.exception()
            elif f.result():
                new.append(s)
        if new:
            self.reindex()
        if failed is not None:
            raise failed
        return new

    def _fetch(self, sid: str) -> bool:
        with self.lock(sid):
            if self.jp2(sid).exists():
                return False                            # another request fetched it meanwhile
            if self.full():                             # a few fetches in flight may pass it together
                raise StoreFull(f"sheet storage full ({self.held_bytes() / 1e9:.0f} GB held, budget "
                                f"{self.budget / 1e9:.0f} GB, {shutil.disk_usage(self.data).free / 1e9:.0f} GB "
                                f"free): {sid} not fetched")
            tmp = Path(tempfile.mkdtemp(prefix=f"{sid}-", dir=self.data / ".tmp"))
            try:
                zpath = tmp / f"{sid}.zip"
                download(f"{self.url}/{sid}.zip", zpath)
                with zipfile.ZipFile(zpath) as z:
                    names = set(z.namelist())
                    if f"{sid}.jp2" not in names or f"{sid}.j2w" not in names:
                        raise SheetMissing(f"{sid}.zip holds {sorted(names)}, not {sid}.jp2 + .j2w")
                    for name in names & {f"{sid}.jp2", f"{sid}.j2w", f"{sid}.gml"}:
                        z.extract(name, tmp)            # CRC-checked as it is read
                for ext in (".j2w", ".gml", ".jp2"):    # the .jp2 last: its existence means a whole sheet
                    src = tmp / f"{sid}{ext}"
                    if src.exists():
                        os.replace(src, self.data / src.name)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            size = sum(p.stat().st_size for p in self._files(sid) if p.exists())
            with self._db() as con:
                con.execute("INSERT OR REPLACE INTO sheets (id, bytes, arrived, in_overview) VALUES (?, ?, ?, 0)",
                            (sid, size, time.time()))
            print(f"[sheets] fetched {sid} ({size / 1e6:.0f} MB)", flush=True)
            return True

    # -- the index MapServer reads ----------------------------------------------------------------
    def reindex(self) -> None:
        with self.lock("index"):
            with self._db() as con:
                held = [r[0] for r in con.execute("SELECT id FROM sheets ORDER BY id")]
            rows = ["location,WKT"]
            for sid in held:
                if self.jp2(sid).exists():
                    e0, n0, e1, n1 = S.sheet_box(sid)
                    rows.append(f'{self.jp2(sid)},"POLYGON(({e0} {n0},{e1} {n0},{e1} {n1},{e0} {n1},{e0} {n0}))"')
            tmp = self.index.with_name(f".{self.index.name}.{os.getpid()}")
            tmp.write_text("\n".join(rows) + "\n")
            os.replace(tmp, self.index)

    def reconcile(self, known) -> None:
        """At start: the record follows the disk. A sheet on disk without a row gets one (and goes into
        the overview again); a row without its sheet goes; leftovers of interrupted fetches go."""
        shutil.rmtree(self.data / ".tmp", ignore_errors=True)
        (self.data / ".tmp").mkdir()
        on_disk = {p.stem for p in self.data.glob("*.jp2") if p.stem in known}
        with self._db() as con:
            rows = {r[0] for r in con.execute("SELECT id FROM sheets")}
            for sid in on_disk - rows:
                size = sum(p.stat().st_size for p in self._files(sid) if p.exists())
                con.execute("INSERT INTO sheets (id, bytes, arrived, in_overview) VALUES (?, ?, ?, 0)",
                            (sid, size, self.jp2(sid).stat().st_mtime))
            con.executemany("DELETE FROM sheets WHERE id = ?", [(s,) for s in rows - on_disk])
        print(f"[sheets] held: {len(on_disk)} ({len(on_disk - rows)} found unrecorded, "
              f"{len(rows - on_disk)} recorded but gone)", flush=True)
        self.reindex()

    # -- the overview's queue ---------------------------------------------------------------------
    def pending_overview(self) -> list[tuple[str, float]]:
        with self._db() as con:
            return list(con.execute("SELECT id, arrived FROM sheets WHERE in_overview = 0 ORDER BY arrived"))

    def mark_overview(self, ids: list[str]) -> None:
        with self._db() as con:
            con.executemany("UPDATE sheets SET in_overview = 1 WHERE id = ?", [(s,) for s in ids])

    # -- the disk budget --------------------------------------------------------------------------
    def held_bytes(self) -> int:
        with self._db() as con:
            return con.execute("SELECT COALESCE(SUM(bytes), 0) FROM sheets").fetchone()[0]


def download(url: str, dest: Path, attempts: int = 4) -> None:
    """url -> dest, retried with a growing pause; the last failure is raised."""
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "earth-ortho (earth.hillview.cz)"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            return
        except OSError as e:
            # a sheet that is not there (HTTP 404, or no such file) stays not there: no retry
            absent = (isinstance(e, urllib.error.HTTPError) and e.code == 404) or (
                isinstance(e, urllib.error.URLError) and isinstance(e.reason, FileNotFoundError))
            if absent or attempt == attempts:
                raise SheetMissing(f"{url}: {e}") from e
            time.sleep(5 * attempt)
