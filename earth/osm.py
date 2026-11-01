"""The map: OpenStreetMap as vector tiles, from one PMTiles archive per release.

An osm release is a file: osm/build.sh runs planetiler with osm/hillview-map.yml over a Geofabrik
extract, the archive is copied into the store's osm/ directory, and `earth release register osm`
records its name, size and sha256. It never changes, so its tiles are immutable. The tiles go out
as the archive holds them (gzipped MVT): buildings, walls, roads and water with their raw OSM tags
and `osm_id`, zoom 13-15 (the archive's header says).

A tile the archive has no entry for, inside its zooms, is empty ground: 204, which a map client
reads as nothing there. Finer than the archive's max zoom is the client's to overzoom: 404.
ODbL: "© OpenStreetMap contributors" (attribution.py).
"""
from __future__ import annotations

import functools
import hashlib
import os
import threading

from pmtiles.reader import MmapSource
from pmtiles.tile import Compression, deserialize_directory, deserialize_header, find_tile, zxy_to_tileid

from . import config

MEDIA_TYPE = "application/vnd.mapbox-vector-tile"


def osm_dir() -> str:
    return os.path.join(config.store_dir(), "osm")


class OsmError(ValueError):
    pass


class Archive:
    """One PMTiles file, its header read once and its directories kept as they are read."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "rb")
        self._get = MmapSource(self._f)
        self.header = deserialize_header(self._get(0, 127))
        if self.header["tile_compression"] not in (Compression.GZIP, Compression.NONE):
            raise OsmError(f"{path}: tiles compressed as {self.header['tile_compression']}, not gzip or none")
        self.gzipped = self.header["tile_compression"] == Compression.GZIP
        self._dir = functools.lru_cache(maxsize=512)(
            lambda off, n: deserialize_directory(self._get(off, n)))

    @property
    def zooms(self) -> tuple[int, int]:
        return self.header["min_zoom"], self.header["max_zoom"]

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """The tile's bytes as stored, or None where the archive has none."""
        tid = zxy_to_tileid(z, x, y)
        off, n = self.header["root_offset"], self.header["root_length"]
        for _ in range(4):
            e = find_tile(self._dir(off, n), tid)
            if e is None:
                return None
            if e.run_length:
                return self._get(self.header["tile_data_offset"] + e.offset, e.length)
            off, n = self.header["leaf_directory_offset"] + e.offset, e.length
        raise OsmError(f"{self.path}: directories deeper than 4 for tile {z}/{x}/{y}")


_lock = threading.Lock()
_open: dict[str, Archive] = {}


def archive(name: str) -> Archive:
    """The release's archive, by its file name under the store's osm/ directory, opened once."""
    with _lock:
        a = _open.get(name)
        if a is None:
            a = _open[name] = Archive(os.path.join(osm_dir(), name))
        return a


def describe(name: str) -> dict:
    """What a release records about its archive: name, bytes, sha256, zooms, bounds."""
    path = os.path.join(osm_dir(), name)
    if os.sep in name or not name.endswith(".pmtiles") or not os.path.isfile(path):
        raise OsmError(f"no archive {name!r} in {osm_dir()}: copy the .pmtiles there first")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    hd = Archive(path).header
    return {"pmtiles": name, "bytes": os.path.getsize(path), "sha256": h.hexdigest(),
            "zooms": [hd["min_zoom"], hd["max_zoom"]],
            "bounds": [hd["min_lon_e7"] / 1e7, hd["min_lat_e7"] / 1e7, hd["max_lon_e7"] / 1e7, hd["max_lat_e7"] / 1e7]}
