"""City models: IPR Praha's official building scene (Budovy 3D 2020, untextured), prepared ahead
of any viewer as georeferenced GLB meshes.

From an earlier hillview sketch (2026-10-15): the I3S decode, the datum conversion, the GLB writer
and every check on them.

Preparing an AREA (`earth models prepare --lat --lon --radius`, at most 2 km): walk the scene's
node hierarchy to the leaves whose bounding spheres meet the circle, fetch their geometry, turn
each vertex from EPSG:4326 + EGM96 heights (EPSG:5773, as the scene declares) into WGS84
ellipsoidal heights and then into an east/up/south frame at the node's centre, and write
content-addressed GLBs of at most 30,000 triangles. The area's manifest is published only once
every asset has succeeded and the scene's revision is read again unchanged. Source bytes are kept
by sha256 under the scene revision, so a later preparation reuses them and never mixes revisions.

A RELEASE is one or more prepared areas (`earth release register models <id> --areas a,b`): one
manifest, frozen, its sha256 recorded. It serves only the assets its manifest names. Nothing is
fetched or converted when a viewer asks.

Only the documented uncompressed I3S triangle layout is accepted; anything else fails the
preparation. The EGM96 grid is explicit (config.egm96_grid), never PROJ's network.
Licence: CC BY 4.0, "datový podklad © IPR Praha" (attribution.py), the change recorded in
every manifest.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import numpy as np

from . import config

BASE = "https://tiles.arcgis.com/tiles/SBTXIEUGWbqzUecw/arcgis/rest/services/budovy3D_2020_2/SceneServer/layers/0"
SOURCE = "https://geoportalpraha.cz/data-a-sluzby/afaedcf7e8b94ed5b1eb4362263bec98"
CREDIT = {"title": "Budovy 3D — IPR Praha", "attribution": "datový podklad © IPR Praha",
          "license": "CC-BY-4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
          "source_url": SOURCE, "changes": "I3S to GLB; EGM96 heights converted to WGS84 ellipsoidal heights"}
GENERATOR = "ipr-i3s-glb-v1"
USER_AGENT = "hillview-earth/1 (+city models)"
TIMEOUT_S = 60
MAX_SOURCE = 32 * 1024 * 1024
MAX_VERTICES = 900_000
CHUNK_VERTICES = 90_000          # 30k triangles: bounds one asset's decode and upload in a browser
MAX_RADIUS_M = 2000
MAX_NODES, MAX_LEAVES, MAX_AREA_ASSETS = 256, 64, 256
MAX_RELEASE_ASSETS = 4096


class CityModelError(ValueError):
    pass


def models_dir() -> Path:
    return Path(config.store_dir()) / "models"


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def atomic(path: Path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".prepare-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read(MAX_SOURCE + 1)
            encoding = (r.headers.get("Content-Encoding") or "identity").lower()
    except urllib.error.HTTPError as e:
        raise CityModelError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CityModelError(f"{url}: {e}") from e
    if len(body) > MAX_SOURCE:
        raise CityModelError(f"source exceeds 32 MiB: {url}")
    # ArcGIS gzips its answers unasked (2026-10-16); the bytes kept are the resource itself
    if encoding == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = d.decompress(body, MAX_SOURCE + 1)
        if len(body) > MAX_SOURCE or d.unconsumed_tail:
            raise CityModelError(f"source exceeds 32 MiB: {url}")
    elif encoding != "identity":
        raise CityModelError(f"{url}: Content-Encoding {encoding}, not gzip or identity")
    return body


class Sources:
    """The scene's bytes, kept by sha256: an index (url -> sha) under `cache`, the bytes in blobs/."""

    def __init__(self, cache: Path, refresh: bool):
        self.cache, self.refresh = cache, refresh

    def get(self, suffix: str, *, current=False) -> bytes:
        url = BASE + suffix
        record = self.cache / "sources" / f"{digest(url.encode())}.json"
        if record.exists() and not (current or self.refresh):
            info = json.loads(record.read_bytes())
            body = (models_dir() / "blobs" / info["sha256"]).read_bytes()
            if digest(body) != info["sha256"]:
                raise CityModelError(f"cached source checksum mismatch: {url}")
            return body
        body = _fetch(url)
        sha = digest(body)
        atomic(models_dir() / "blobs" / sha, body)
        info = {"url": url, "sha256": sha, "bytes": len(body), "fetched_at": datetime.now(timezone.utc).isoformat()}
        # each version's provenance as well as the acquisition index
        atomic(models_dir() / "provenance" / f"{sha}.json", encoded(info))
        atomic(record, encoded(info))
        return body


def validate_layer(layer):
    if layer["spatialReference"].get("wkid") != 4326 or layer["spatialReference"].get("vcsWkid") != 5773:
        raise CityModelError("IPR source must declare EPSG:4326 horizontal / EPSG:5773 EGM96 vertical")
    if layer["heightModelInfo"] != {"heightModel": "gravity_related_height", "vertCRS": "EGM96_Geoid", "heightUnit": "meter"}:
        raise CityModelError("unsupported IPR height model")
    expected = {"geometryType": "triangles", "header": [{"property": "vertexCount", "type": "UInt32"},
                {"property": "featureCount", "type": "UInt32"}], "topology": "PerAttributeArray",
                "ordering": ["position", "normal", "uv0", "color"],
                "vertexAttributes": {"position": {"valueType": "Float32", "valuesPerElement": 3},
                    "normal": {"valueType": "Float32", "valuesPerElement": 3},
                    "uv0": {"valueType": "Float32", "valuesPerElement": 2},
                    "color": {"valueType": "UInt8", "valuesPerElement": 4}},
                "featureAttributeOrder": ["id", "faceRange"], "featureAttributes": {
                    "id": {"valueType": "UInt64", "valuesPerElement": 1},
                    "faceRange": {"valueType": "UInt32", "valuesPerElement": 2}}}
    if layer["store"]["defaultGeometrySchema"] != expected:
        raise CityModelError("unsupported IPR geometry schema; update the converter explicitly")


def ecef(lon, lat, h):
    lon, lat = np.radians(lon), np.radians(lat)
    e2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
    n = 6378137 / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.stack(((n + h) * np.cos(lat) * np.cos(lon),
                     (n + h) * np.cos(lat) * np.sin(lon),
                     (n * (1 - e2) + h) * np.sin(lat)), axis=-1)


def axes(lon, lat):
    lo, la = math.radians(lon), math.radians(lat)
    # GLB axes east/up/south, a right-handed basis
    return np.array([[-math.sin(lo), math.cos(lo), 0],
                     [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)],
                     [math.sin(la) * math.cos(lo), math.sin(la) * math.sin(lo), -math.cos(la)]])


class Heights:
    """EGM96 orthometric -> WGS84 ellipsoidal, from the grid file named, and nothing else."""

    def __init__(self, grid: Path):
        from pyproj import Transformer
        grid = Path(grid).resolve(strict=True)
        self.sha = digest(grid.read_bytes())
        self.transform = Transformer.from_pipeline(
            f"+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            f"+step +proj=vgridshift +grids={grid} +multiplier=1 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg")

    def ellipsoid(self, lon, lat, h):
        # the explicitly positive multiplier adds the grid's undulation: h = H + N
        return self.transform.transform(lon, lat, h, errcheck=True)[2]


def decode(body: bytes, node, heights: Heights):
    if len(body) < 8:
        raise CityModelError("truncated I3S header")
    count, features = struct.unpack_from("<II", body)
    if not 0 < count <= MAX_VERTICES or count % 3 or features > count // 3:
        raise CityModelError("invalid or oversized I3S counts")
    if len(body) != 8 + count * 36 + features * 16:
        raise CityModelError("I3S geometry length does not match the declared layout")
    positions = np.frombuffer(body, "<f4", count * 3, 8).reshape(-1, 3).astype(np.float64)
    center = np.array(node["obb"]["center"], dtype=np.float64)
    if center.shape != (3,) or not np.isfinite(center).all() or not np.isfinite(positions).all():
        raise CityModelError("nonfinite I3S coordinates")
    geographic = positions + center
    if (np.abs(geographic[:, 0]) > 180).any() or (np.abs(geographic[:, 1]) > 90).any():
        raise CityModelError("I3S geographic coordinates out of range")
    anchor_h = float(heights.ellipsoid(*center))
    h = heights.ellipsoid(geographic[:, 0], geographic[:, 1], geographic[:, 2])
    xyz = (ecef(geographic[:, 0], geographic[:, 1], h) - ecef(*center[:2], anchor_h)) @ axes(*center[:2]).T
    if not np.isfinite(xyz).all() or np.max(np.abs(xyz)) > 20_000:
        raise CityModelError("I3S geometry outside the supported local extent")
    colors = np.frombuffer(body, "u1", count * 4, 8 + count * 32).reshape(-1, 4)[:, :3] / 255
    colors = np.where(colors <= .04045, colors / 12.92, ((colors + .055) / 1.055) ** 2.4)
    faces = xyz.reshape(-1, 3, 3)
    normal = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    lengths = np.linalg.norm(normal, axis=1)
    normal /= np.maximum(lengths, 1e-20)[:, None]
    normals = np.repeat(normal, 3, axis=0)
    return xyz.astype("<f4"), normals.astype("<f4"), colors.astype("<f4"), [float(center[1]), float(center[0]), anchor_h]


def glb(positions, normals, colors) -> bytes:
    arrays = [np.ascontiguousarray(a, dtype="<f4") for a in (positions, normals, colors)]
    binary = b"".join(a.tobytes() for a in arrays)
    views, offset = [], 0
    for a in arrays:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": a.nbytes, "target": 34962})
        offset += a.nbytes
    accessors = [{"bufferView": i, "componentType": 5126, "count": len(a), "type": "VEC3"} for i, a in enumerate(arrays)]
    accessors[0].update(min=positions.min(0).tolist(), max=positions.max(0).tolist())
    doc = {"asset": {"version": "2.0", "generator": GENERATOR}, "buffers": [{"byteLength": len(binary)}],
           "bufferViews": views, "accessors": accessors,
           "materials": [{"doubleSided": True, "pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 1}}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "COLOR_0": 2}, "material": 0}]}],
           "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}], "scene": 0}
    js = encoded(doc)
    js += b" " * (-len(js) % 4)
    return (struct.pack("<5I", 0x46546C67, 2, 28 + len(js) + len(binary), len(js), 0x4E4F534A)
            + js + struct.pack("<2I", len(binary), 0x004E4942) + binary)


def prepare(grid: Path, lat: float, lon: float, radius: float, refresh=False) -> tuple[str, dict]:
    """Prepare one area; (its manifest's sha256, the manifest). Raises CityModelError."""
    if (not all(math.isfinite(v) for v in (lat, lon, radius)) or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or not 0 < radius <= MAX_RADIUS_M):
        raise CityModelError(f"require finite lat/lon and a radius in (0, {MAX_RADIUS_M}] metres")
    heights = Heights(grid)
    source = Sources(models_dir() / "ipr", refresh)
    layer_body = source.get("?f=json", current=True)
    layer = json.loads(layer_body)
    validate_layer(layer)
    root_body = source.get("/nodes/root?f=json", current=True)
    root_node = json.loads(root_body)
    revision = root_node["version"]
    UUID(revision)  # the scene's identity is also a directory name
    # source indexes per scene revision: never reuse nodes from another scene
    source.cache = models_dir() / "ipr" / "acquisitions" / revision
    pending, visited, leaves = [root_node], set(), []
    target = ecef(lon, lat, 0)
    while pending:
        node = pending.pop()
        if node["id"] in visited:
            raise CityModelError("I3S hierarchy contains a repeated node")
        visited.add(node["id"])
        if len(visited) > MAX_NODES:
            raise CityModelError(f"the area exceeds the {MAX_NODES}-node preparation budget")
        if node["version"] != revision:
            raise CityModelError("IPR scene changed during preparation; retry with --refresh")
        if not node.get("children"):
            if node.get("textureData"):
                raise CityModelError("textured IPR nodes are not supported by this converter")
            if node.get("geometryData") != [{"href": "./geometries/0"}]:
                raise CityModelError("unsupported IPR leaf geometry layout")
            leaves.append(node)
            if len(leaves) > MAX_LEAVES:
                raise CityModelError(f"the area exceeds the {MAX_LEAVES}-leaf preparation budget")
        else:
            for child in node["children"]:
                if not isinstance(child["id"], str) or not child["id"].isascii() or not child["id"].isdigit():
                    raise CityModelError("invalid I3S child identity")
                c = child["mbs"]
                if np.linalg.norm(ecef(c[0], c[1], 0) - target) <= radius + c[3]:
                    pending.append(json.loads(source.get(f"/nodes/{child['id']}?f=json")))
    assets, provenance = [], []
    for node in sorted(leaves, key=lambda n: int(n["id"])):
        body = source.get(f"/nodes/{node['id']}/geometries/0")
        xyz, normals, colors, anchor = decode(body, node, heights)
        provenance.append({"node": node["id"], "node_sha256": digest(encoded(node)), "geometry_sha256": digest(body)})
        for start in range(0, len(xyz), CHUNK_VERTICES):
            end = start + CHUNK_VERTICES
            model = glb(xyz[start:end], normals[start:end], colors[start:end])
            sha = digest(model)
            atomic(models_dir() / "assets" / f"{sha}.glb", model)
            assets.append({"id": f"ipr/{revision}/{node['id']}/{start // CHUNK_VERTICES}",
                           "url": f"assets/{sha}.glb", "sha256": sha, "bytes": len(model),
                           "triangles": len(xyz[start:end]) // 3, "anchor": anchor,
                           "radius": node["mbs"][3], "node": node["id"]})
            if len(assets) > MAX_AREA_ASSETS:
                raise CityModelError(f"the area exceeds the {MAX_AREA_ASSETS}-asset budget")
    area = {"schema": 2, "generator": GENERATOR, "source": BASE, "revision": revision,
            "credit": CREDIT, "source_datum": "EPSG:4326+5773", "output_datum": "WGS84 ellipsoidal",
            "geoid_sha256": heights.sha, "layer_sha256": digest(layer_body),
            "root_sha256": digest(root_body), "provenance": provenance,
            "area": {"lat": lat, "lon": lon, "radius": radius}, "assets": assets}
    # a second root read catches an update during acquisition: no partial publication
    if json.loads(source.get("/nodes/root?f=json", current=True))["version"] != revision:
        raise CityModelError("IPR scene changed during preparation; the area is not published")
    body = encoded(area)
    sha = digest(body)
    atomic(models_dir() / "areas" / f"{sha}.json", body)
    return sha, area


def compose(area_shas: list[str]) -> tuple[str, dict]:
    """A release's manifest from prepared areas (one scene revision): every area's assets once,
    each asset present on disk with its sha256. (its sha256, the manifest)"""
    if not area_shas:
        raise CityModelError("a models release is one or more prepared areas (--areas)")
    areas = []
    for sha in area_shas:
        p = models_dir() / "areas" / f"{sha}.json"
        if len(sha) != 64 or not p.is_file():
            raise CityModelError(f"no prepared area {sha!r} (earth models prepare prints it)")
        body = p.read_bytes()
        if digest(body) != sha:
            raise CityModelError(f"prepared area {sha} does not match its sha256")
        areas.append(json.loads(body))
    revisions = {a["revision"] for a in areas}
    if len(revisions) != 1:
        raise CityModelError(f"the areas are of different scene revisions: {sorted(revisions)}")
    assets: dict[str, dict] = {}
    for a in areas:
        for asset in a["assets"]:
            f = models_dir() / "assets" / f"{asset['sha256']}.glb"
            if not f.is_file() or digest(f.read_bytes()) != asset["sha256"]:
                raise CityModelError(f"asset {asset['sha256']} is missing or corrupt")
            assets[asset["id"]] = asset
    if len(assets) > MAX_RELEASE_ASSETS:
        raise CityModelError(f"{len(assets)} assets: a release holds at most {MAX_RELEASE_ASSETS}")
    first = areas[0]
    manifest = {"schema": 2, "generator": GENERATOR, "source": first["source"], "revision": first["revision"],
                "credit": first["credit"], "source_datum": first["source_datum"],
                "output_datum": first["output_datum"], "geoid_sha256": first["geoid_sha256"],
                "areas": [{"sha256": s, **a["area"]} for s, a in zip(area_shas, areas)],
                "assets": [assets[k] for k in sorted(assets)]}
    body = encoded(manifest)
    sha = digest(body)
    atomic(models_dir() / "manifests" / f"{sha}.json", body)
    return sha, manifest


def describe(area_shas: list[str]) -> dict:
    """What a models release records: its manifest's sha256, the areas, the asset count."""
    sha, m = compose(area_shas)
    return {"manifest": sha, "revision": m["revision"], "areas": area_shas, "assets": len(m["assets"]),
            "triangles": sum(a["triangles"] for a in m["assets"]), "generator": GENERATOR}


class Release:
    """One registered release's manifest, read and checked once, and the assets it may serve."""

    def __init__(self, manifest_sha: str):
        p = models_dir() / "manifests" / f"{manifest_sha}.json"
        body = p.read_bytes()
        if digest(body) != manifest_sha:
            raise CityModelError(f"manifest {manifest_sha} does not match its sha256")
        self.body = body
        self.assets = {a["sha256"] for a in json.loads(body)["assets"]}

    def asset_path(self, sha: str) -> Path | None:
        return models_dir() / "assets" / f"{sha}.glb" if sha in self.assets else None
