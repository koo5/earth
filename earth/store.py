"""Releases, channels, and the tiles kept under them.

A RELEASE is an immutable set: one layer's data and the generator that made it. Its id is in every
URL under it, so its responses are `Cache-Control: immutable` and a CDN in front needs no
invalidation. A CHANNEL is a named, mutable pointer: which release of each layer to serve. There
is one world, so two channels, `prod` (what hillview shows) and `beta` (the workbench), plus any
an operator registers. Clients ask for a channel once and then only for release URLs.

A terrain release is made ON DEMAND: a tile is made the first time it is asked for and kept. That
is still a fixed set as long as the mosaics it reads do not change, so a release records the
mosaics' stamp (tiles.source_stamp) when it is registered and refuses to make a tile from any
other ("frozen"). New ČÚZK sheets mean a new release, not changed tiles. A `live` release instead
follows the mosaics as they are -- for a workbench whose ground is still being built: its tiles
are kept per stamp and served as short-lived, never as immutable.

Publishing is by hand: `earth release
register`, `earth channel set` (cli.py). No publish API.

Kept tiles live in SQLite packs, one per zoom-10 block per model -- CZ to zoom 18 is eleven million
tiles, fine for ext4 but slow to copy, back up, or delete as a release.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass

from . import config

LAYERS = ("terrain", "osm", "ortho", "models", "names", "solves")
RELEASE_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
CHANNEL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PACK_Z = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY, layer TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL,
  generator TEXT NOT NULL, sources TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS channels (
  name TEXT PRIMARY KEY, bundle TEXT NOT NULL, moved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_moves (
  name TEXT NOT NULL, bundle TEXT NOT NULL, moved_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
"""
_PACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
  z INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, fmt TEXT NOT NULL,
  stamp TEXT NOT NULL, body BLOB NOT NULL, meta TEXT NOT NULL,
  PRIMARY KEY (z, x, y, fmt, stamp));
"""


class StoreError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def stamp_key(sources: dict) -> str:
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Release:
    id: str
    layer: str
    mode: str                 # "frozen" | "live"
    created_at: str
    generator: str
    sources: dict             # terrain: {model: tiles.source_stamp(model)}
    note: str

    @property
    def immutable(self) -> bool:
        return self.mode == "frozen"


class Store:
    def __init__(self, root: str | None = None):
        self.root = root or config.store_dir()
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as c:
            c.executescript(_SCHEMA)

    # --- the catalogue -----------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(os.path.join(self.root, "earth.sqlite"), timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    def register(self, id: str, layer: str, sources: dict, generator: str, mode: str = "frozen",
                 note: str = "") -> Release:
        if not RELEASE_ID.match(id):
            raise StoreError(f"release id {id!r}: lower-case letters, digits and single dashes")
        if layer not in LAYERS:
            raise StoreError(f"layer must be one of {LAYERS}")
        if mode not in ("frozen", "live"):
            raise StoreError("mode must be frozen or live")
        with self._lock, self._db() as c:
            if c.execute("SELECT 1 FROM releases WHERE id = ?", (id,)).fetchone():
                raise StoreError(f"release {id} exists: a release never changes; register a new id")
            c.execute("INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (id, layer, mode, _now(), generator, json.dumps(sources, sort_keys=True), note))
        return self.release(id)  # type: ignore[return-value]

    def release(self, id: str) -> Release | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM releases WHERE id = ?", (id,)).fetchone()
        if not r:
            return None
        return Release(r["id"], r["layer"], r["mode"], r["created_at"], r["generator"],
                       json.loads(r["sources"]), r["note"])

    def releases(self) -> list[Release]:
        with self._db() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM releases ORDER BY created_at, id")]
        return [r for r in (self.release(i) for i in ids) if r]

    def set_channel(self, name: str, bundle: dict, note: str = "") -> dict:
        """Point `name` at the releases in `bundle` ({layer: release id}); layers not named keep
        what the channel had."""
        if not CHANNEL_NAME.match(name):
            raise StoreError(f"channel name {name!r}")
        for layer, rid in bundle.items():
            r = self.release(rid)
            if r is None:
                raise StoreError(f"no release {rid}")
            if r.layer != layer:
                raise StoreError(f"release {rid} is a {r.layer} release, not {layer}")
        with self._lock, self._db() as c:
            row = c.execute("SELECT bundle FROM channels WHERE name = ?", (name,)).fetchone()
            merged = {**(json.loads(row["bundle"]) if row else {}), **bundle}
            now = _now()
            c.execute("INSERT INTO channels VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                      "bundle = excluded.bundle, moved_at = excluded.moved_at",
                      (name, json.dumps(merged, sort_keys=True), now))
            c.execute("INSERT INTO channel_moves VALUES (?, ?, ?, ?)",
                      (name, json.dumps(merged, sort_keys=True), now, note))
        return self.channel(name)  # type: ignore[return-value]

    def channel(self, name: str) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
        return None if not r else {"name": r["name"], "releases": json.loads(r["bundle"]), "moved_at": r["moved_at"]}

    def channels(self) -> list[dict]:
        with self._db() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM channels ORDER BY name")]
        return [ch for ch in (self.channel(n) for n in names) if ch]

    # --- kept tiles ---------------------------------------------------------------------------

    def _pack(self, release: str, model: str, z: int, x: int, y: int, layer: str = "terrain") -> str:
        block = "low" if z <= PACK_Z else f"{x >> (z - PACK_Z)}-{y >> (z - PACK_Z)}"
        return os.path.join(self.root, "releases", release, layer, model, f"{block}.sqlite")

    def _pack_db(self, path: str, create: bool) -> sqlite3.Connection | None:
        if not create and not os.path.exists(path):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_PACK_SCHEMA)
        return c

    def get_tile(self, release: str, model: str, z: int, x: int, y: int, fmt: str, stamp: str,
                 layer: str = "terrain") -> tuple[bytes, dict] | None:
        c = self._pack_db(self._pack(release, model, z, x, y, layer), create=False)
        if c is None:
            return None
        with c:
            r = c.execute("SELECT body, meta FROM tiles WHERE z=? AND x=? AND y=? AND fmt=? AND stamp=?",
                          (z, x, y, fmt, stamp)).fetchone()
        c.close()
        return None if r is None else (r[0], json.loads(r[1]))

    def put_tile(self, release: str, model: str, z: int, x: int, y: int, fmt: str, stamp: str,
                 body: bytes, meta: dict, layer: str = "terrain") -> None:
        c = self._pack_db(self._pack(release, model, z, x, y, layer), create=True)
        assert c is not None
        with c:
            c.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (z, x, y, fmt, stamp, body, json.dumps(meta)))
        c.close()
