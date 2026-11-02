"""Names: OSM's named peaks, towers and places, each at an ellipsoidal height, for a viewer's labels (recon ea07).

A names release is one JSON file built ahead (`earth names build`): osm/names.sh pulls the named points out of the
same Geofabrik extract the map is made from (osmium-tool), and this module turns them into what a viewer labels
with -- the set the workbench's /terrain/labels has drawn since 2026-10-02: peaks, observation towers,
communication masts and places from city down to quarter, each with `ele` and `prominence` from its tags (ele is
orthometric) and a place's `population`. A missing `ele` is the surface model there (ČÚZK DSM10 where built, else
GLO-30), marked ele_estimated, as the workbench fills it. `h` is ele + N (EGM2008): ellipsoidal, as everything
earth serves. A name with no height at all stays out: a viewer cannot place it.

The file lives in the store's names/ directory; the release records its name, size and sha256. A query is a disc
round a point, cut into near-first chunks, answered as /terrain/labels answers. ODbL: "© OpenStreetMap
contributors" (attribution.py).
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import threading
from collections.abc import Callable, Iterable

import numpy as np

from . import config

GENERATOR = "earth names v1"
R_EARTH_M = 6_371_000.0
MAX_RADIUS_M = 200_000.0
MAX_CHUNKS = 64
# the index's cells, degrees: a 120 km disc touches a few hundred
CELL_DEG = 0.1


class NamesError(ValueError):
    pass


def names_dir() -> str:
    return os.path.join(config.store_dir(), "names")


# ---- copied from the workbench's enrich/api/app/routers/terrain.py (2026-11-02), as they are: what a name is and
# how its tags read. Keep the two in step; tests/test_names.py pins the copies' behaviour.

def parse_ele(v) -> float | None:
    """OSM ele/prominence values arrive as '1602', '1602.4', '1602 m', '1,602'…"""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def parse_pop(v) -> int | None:
    """OSM population arrives as '1324277', '1 324 277', '1,324,277'…"""
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").replace(" ", "").replace(" ", "")))
    except ValueError:
        return None


def features_from_overpass(data: dict) -> list[dict]:
    """Overpass JSON (peaks ∪ towers/masts ∪ places union) → candidate dicts.
    kind: peak | tower (observation) | mast (communication) | city | town |
    village | suburb | quarter. prominence rides along where OSM has it (~2%
    of peaks — but precisely the famous ones); population where places have
    it (~98% around Prague, probed 2026-07-28): the client uses both for
    label priority now that the pool is uncapped."""
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name, lat, lon = tags.get("name"), el.get("lat"), el.get("lon")
        if not name or lat is None or lon is None:
            continue
        if tags.get("place"):
            kind = tags["place"]
        elif tags.get("natural") == "peak":
            kind = "peak"
        elif tags.get("tower:type") == "observation":
            kind = "tower"
        else:
            kind = "mast"
        f = {"name": name, "lat": lat, "lon": lon, "kind": kind,
             "ele": parse_ele(tags.get("ele"))}
        prom = parse_ele(tags.get("prominence"))
        if prom is not None:
            f["prominence"] = prom
        pop = parse_pop(tags.get("population"))
        if pop is not None:
            f["population"] = pop
        out.append(f)
    return out


def in_query(tags: dict) -> bool:
    """The workbench's Overpass query (its _TILE_UNION) as a test of one node's tags; features_from_overpass relies
    on it ("else: mast" is a communication mast only because the query admitted nothing else):
        node["natural"="peak"]["name"];
        node["man_made"~"^(tower|mast)$"]["tower:type"~"^(observation|communication)$"]["name"];
        node["place"~"^(city|town|village|suburb|quarter)$"]["name"];"""
    if not tags.get("name"):
        return False
    return (tags.get("natural") == "peak"
            or (tags.get("man_made") in ("tower", "mast") and tags.get("tower:type") in ("observation", "communication"))
            or tags.get("place") in ("city", "town", "village", "suburb", "quarter"))


def water_tower(tags: dict) -> bool:
    """Beyond the workbench's query (the operator, 2026-11-02: "water towers sound useful too"): a named water
    tower, as OSM tags it (man_made=water_tower; the rarer man_made=tower + tower:type=water). Its own kind,
    `water_tower`: a local landmark, which the viewer reaches and hides as it does a street's name, not a peak's."""
    return bool(tags.get("name")) and (tags.get("man_made") == "water_tower"
                                       or (tags.get("man_made") == "tower" and tags.get("tower:type") == "water"))


def features(lines: Iterable[str]) -> list[dict]:
    """GeoJSON text sequence (osmium export -f geojsonseq; a record separator before each record is allowed) ->
    the workbench's candidate records: each node its query would return (in_query), passed through
    features_from_overpass as the Overpass element it would have been; and the water towers beside them."""
    elements, towers = [], []
    for line in lines:
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        f = json.loads(line)
        g = f.get("geometry") or {}
        tags = f.get("properties") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = (round(float(c), 7) for c in g["coordinates"][:2])
        if in_query(tags):
            elements.append({"type": "node", "lat": lat, "lon": lon, "tags": tags})
        elif water_tower(tags):
            towers.append({"name": tags["name"], "lat": lat, "lon": lon, "kind": "water_tower",
                           "ele": parse_ele(tags.get("ele"))})
    return features_from_overpass({"elements": elements}) + towers


Surface = Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]
Geoid = Callable[[np.ndarray, np.ndarray], np.ndarray]


def surface_heights(lons: np.ndarray, lats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The surface model's orthometric heights at the points, and which it had. Refuses when any lies under a ČÚZK
    sheet not built on this machine: a release made there would leave out every name it could not place (on dev44,
    2026-11-02, 14,256 of 25,174), and nothing in it would say so. Build names where the surface is complete."""
    from . import ground
    g = ground.sample_ground(lons, lats, ground.TERRAIN_SURFACE)
    c = g.counts()
    if c["not_fetched"]:
        raise NamesError(f"{c['not_fetched']} of {len(lons)} names without an ele lie under surface sheets not built "
                         f"here ({ground.TERRAIN_SURFACE}): build the names where the mosaics are complete")
    return g.height, g.ok


def with_heights(feats: list[dict], surface: Surface | None = None, geoid: Geoid | None = None) -> list[dict]:
    """Each record with `h` (ellipsoidal), as the workbench makes it: a missing `ele` filled from the surface model
    and marked ele_estimated (its fill_missing_ele, here over earth's own mosaics), then h = ele + N (its
    /terrain/labels). A record still without a height is dropped here rather than served with h None, which
    LabelPool drops anyway."""
    if surface is None:
        surface = surface_heights
    if geoid is None:
        from .datum import geoid_heights as geoid
    missing = [f for f in feats if f["ele"] is None]
    if missing:
        hs, ok = surface(np.array([f["lon"] for f in missing]), np.array([f["lat"] for f in missing]))
        for f, h, good in zip(missing, hs, ok):
            if good and math.isfinite(float(h)):
                f["ele"] = round(float(h), 1)
                f["ele_estimated"] = True
    placed = [f for f in feats if f["ele"] is not None]
    if not placed:
        return []
    n = geoid(np.array([f["lat"] for f in placed]), np.array([f["lon"] for f in placed]))
    return [{**f, "h": round(f["ele"] + float(nn), 2)} for f, nn in zip(placed, n)]


def build(geojsonseq: str, out: str, surface: Surface | None = None, geoid: Geoid | None = None) -> dict:
    """names.sh's export -> the release file (`out`, under names_dir() for a release); what was built."""
    with open(geojsonseq, encoding="utf-8") as fh:
        feats = features(fh)
    named = with_heights(feats, surface, geoid)
    named.sort(key=lambda f: (f["lat"], f["lon"], f["kind"], f["name"]))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"generator": GENERATOR, "names": named}, fh, ensure_ascii=False, separators=(",", ":"))
    kinds: dict[str, int] = {}
    for f in named:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    return {"read": len(feats), "kept": len(named), "estimated": sum(1 for f in named if f.get("ele_estimated")),
            "kinds": kinds}


def describe(name: str) -> dict:
    """What a release records about its file: name, bytes, sha256, how many names."""
    path = os.path.join(names_dir(), name)
    if os.sep in name or not name.endswith(".json") or not os.path.isfile(path):
        raise NamesError(f"no names file {name!r} in {names_dir()}: build it there first (earth names build)")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"names": name, "bytes": os.path.getsize(path), "sha256": h.hexdigest(), "count": len(Names(path).all)}


# copied from the workbench's routers/terrain.py, as above
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    la1, la2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(a)))


class Names:
    """One release's names, read once and indexed by cell."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("generator") != GENERATOR or not isinstance(doc.get("names"), list):
            raise NamesError(f"{path}: not a {GENERATOR} file")
        self.all: list[dict] = doc["names"]
        self.cells: dict[tuple[int, int], list[dict]] = {}
        for f in self.all:
            self.cells.setdefault(self._cell(f["lat"], f["lon"]), []).append(f)

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / CELL_DEG), math.floor(lon / CELL_DEG)

    def disc(self, lat: float, lon: float, radius_m: float, chunk: int | None = None, chunks: int = 1) -> dict:
        """The names within radius_m of (lat, lon); with `chunk`, slice `chunk` of `chunks` by distance, nearest
        first, so a viewer streams the near ones before the far ones."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise NamesError(f"no point {lat}, {lon}")
        radius_m = max(1_000.0, min(radius_m, MAX_RADIUS_M))
        dlat = math.degrees(radius_m / R_EARTH_M)
        dlon = dlat / max(0.01, math.cos(math.radians(lat)))
        (c0, d0), (c1, d1) = self._cell(lat - dlat, lon - dlon), self._cell(lat + dlat, lon + dlon)
        found = []
        for ci in range(c0, c1 + 1):
            for cj in range(d0, d1 + 1):
                for f in self.cells.get((ci, cj), ()):
                    d = _haversine_m(lat, lon, f["lat"], f["lon"])
                    if d <= radius_m:
                        found.append((d, f))
        found.sort(key=lambda x: x[0])
        out: dict = {}
        if chunk is not None:
            n = max(1, min(MAX_CHUNKS, chunks))
            i = max(0, min(chunk, n - 1))
            k = len(found)
            found = found[i * k // n:(i + 1) * k // n]
            out = {"chunk": i, "chunks": n}
        # highest first within what is answered, as the workbench's pool sorts its result
        return {"labels": sorted((f for _, f in found), key=lambda f: f["ele"], reverse=True), **out}


_lock = threading.Lock()


@functools.lru_cache(maxsize=4)
def _load(path: str) -> Names:
    return Names(path)


def release(name: str) -> Names:
    """The release's names, by its file name under the store's names/ directory, read once."""
    with _lock:
        return _load(os.path.join(names_dir(), name))
