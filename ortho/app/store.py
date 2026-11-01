"""The sheets held on disk: fetched when a detail tile first needs them, indexed for MapServer, evicted by
last use under a disk budget.

- A sheet is downloaded once, by whichever request asks first (a lock per sheet; several gunicorn workers),
  from ČÚZK's open-data endpoint as <id>.zip, and published as get_tiles.sh did: sidecars first, the .jp2
  last by an atomic rename, so a reader never sees half a sheet.
- MapServer finds the held sheets through a tile index, sheets.csv (location + its exact 2 km square as WKT,
  read by OGR's CSV driver), rewritten atomically on every change. Nothing reads a JP2 to index it.
- sheets.sqlite records each sheet's size, arrival, last use and whether the overview has it yet.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sheets as S

TOUCH_EVERY_S = 60          # last use is a day-scale signal: one write a minute per sheet is plenty


class SheetMissing(RuntimeError):
    pass


class Store:
    def __init__(self, data_dir: str, state_dir: str, sheets_url: str, fetch_jobs: int = 4):
        self.data, self.state = Path(data_dir), Path(state_dir)
        self.url = sheets_url.rstrip("/")
        self.jobs = fetch_jobs
        self.db_path = self.state / "sheets.sqlite"
        self.index = self.state / "sheets.csv"
        for d in (self.data / ".locks", self.data / ".tmp", self.state):
            d.mkdir(parents=True, exist_ok=True)
        with self._db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS sheets (id TEXT PRIMARY KEY, bytes INTEGER NOT NULL, "
                        "arrived REAL NOT NULL, last_used REAL NOT NULL, in_overview INTEGER NOT NULL DEFAULT 0)")

    # -- plumbing ---------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextlib.contextmanager
    def lock(self, name: str, shared: bool = False):
        with open(self.data / ".locks" / f"{name}.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield

    def jp2(self, sid: str) -> Path:
        return self.data / f"{sid}.jp2"

    def _files(self, sid: str) -> list[Path]:
        return [self.data / f"{sid}{ext}" for ext in (".jp2", ".j2w", ".gml")]

    # -- fetching ---------------------------------------------------------------------------------
    @contextlib.contextmanager
    def holding(self, ids: list[str]):
        """While the body runs (a tile render), every sheet in `ids` is on disk, indexed, and cannot be
        evicted: a shared lock per sheet, taken BEFORE looking, which eviction's exclusive lock waits for.
        Missing ones are fetched first (locks dropped meanwhile, so no render waits on a download it does
        not need). A sheet that cannot be fetched raises: a detail tile is never drawn with one missing."""
        ids = sorted(set(ids))
        while True:
            with contextlib.ExitStack() as held:
                for s in ids:
                    held.enter_context(self.lock(s, shared=True))
                missing = [s for s in ids if not self.jp2(s).exists()]
                if not missing:
                    self.touch(ids)
                    yield
                    return
            self.fetch(missing)

    def fetch(self, ids: list[str]) -> list[str]:
        """Fetch the sheets in `ids` not on disk yet, several at once; the ones this call fetched."""
        with ThreadPoolExecutor(max(1, min(self.jobs, len(ids)))) as pool:
            results = list(pool.map(self._fetch, ids))
        new = [s for s, fetched in zip(ids, results) if fetched]
        if new:
            self.reindex()
        return new

    def _fetch(self, sid: str) -> bool:
        with self.lock(sid):
            if self.jp2(sid).exists():
                return False                            # another request fetched it meanwhile
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
            now = time.time()
            with self._db() as con:
                con.execute("INSERT OR REPLACE INTO sheets VALUES (?, ?, ?, ?, 0)", (sid, size, now, now))
            print(f"[sheets] fetched {sid} ({size / 1e6:.0f} MB)", flush=True)
            return True

    def touch(self, ids: list[str]) -> None:
        if not ids:
            return
        now = time.time()
        with self._db() as con:
            con.executemany("UPDATE sheets SET last_used = ? WHERE id = ? AND last_used < ?",
                            [(now, s, now - TOUCH_EVERY_S) for s in ids])

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
                mtime = self.jp2(sid).stat().st_mtime
                con.execute("INSERT INTO sheets VALUES (?, ?, ?, ?, 0)", (sid, size, mtime, mtime))
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

    def evict(self, high: int, low: int) -> list[str]:
        """Past `high` bytes, the least recently used sheets go until `low` is reached (a hysteresis: no
        sheet-by-sheet churn at the line). A sheet the overview has not taken yet stays. Out of the index
        first, then off the disk; the overview keeps its pixels."""
        total = self.held_bytes()
        if total <= high:
            return []
        gone = []
        with self._db() as con:
            rows = list(con.execute("SELECT id, bytes FROM sheets WHERE in_overview = 1 ORDER BY last_used"))
        for sid, size in rows:
            if total <= low:
                break
            with self.lock(sid):                    # waits for renders holding it (holding())
                with self._db() as con:
                    con.execute("DELETE FROM sheets WHERE id = ?", (sid,))
                self.reindex()
                for p in self._files(sid):
                    p.unlink(missing_ok=True)
            gone.append(sid)
            total -= size
        if gone:
            print(f"[sheets] evicted {len(gone)} (least recently used), {total / 1e9:.0f} GB held", flush=True)
        return gone


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
