"""City models: the conversion (from the earlier sketch's tests), preparation against a
scene faked at the fetch, a release composed of prepared areas, and the two routes that serve it."""
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer

from earth import app as app_mod, citymodels as cm, cli, limits, store

REV = "f0661867-e92a-4e48-b041-a2b95ca11b9a"
CENTER = [14.4, 50.09, 279]


def geometry(points):
    n = len(points)
    return (struct.pack("<II", n, 1) + np.array(points, dtype="<f4").tobytes()
            + bytes(n * 20) + bytes([255, 128, 0, 255]) * n + struct.pack("<QII", 1, 0, n // 3 - 1))


GEO = geometry([[0, 0, 0], [.0001, 0, 0], [0, 0, 10]])


@pytest.fixture
def grid(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    # PROJ GTX: 4 x 4, a constant +45 m undulation over Prague
    g = tmp_path / "positive45.gtx"
    g.write_bytes(struct.pack(">ddddii", 49, 13, 1, 1, 4, 4) + struct.pack(">16f", *([45] * 16)))
    monkeypatch.setenv("EARTH_EGM96_GRID", str(g))
    return g


@pytest.fixture
def heights(grid):
    return cm.Heights(grid)


class Scene:
    """The IPR scene at the fetch: a root with leaves 925 and 926; `changed` moves the revision
    on every root read after the first."""

    def __init__(self, monkeypatch):
        self.fetched: list[str] = []
        self.changed = False
        self.roots = 0
        monkeypatch.setattr(cm, "_fetch", self.fetch)
        monkeypatch.setattr(cm, "validate_layer", lambda layer: None)

    def leaf(self, id_, lon):
        c = [lon, CENTER[1], CENTER[2]]
        return {"id": id_, "mbs": [*c, 100], "version": REV, "obb": {"center": c},
                "geometryData": [{"href": "./geometries/0"}]}

    def fetch(self, url):
        suffix = url[len(cm.BASE):]
        self.fetched.append(suffix)
        if suffix == "?f=json":
            return b"{}"
        if suffix == "/nodes/root?f=json":
            self.roots += 1
            v = "changed" if self.changed and self.roots > 1 else REV
            kids = [{"id": "925", "mbs": [14.4, 50.09, 279, 100]}, {"id": "926", "mbs": [14.43, 50.09, 279, 100]}]
            return json.dumps({"id": "root", "version": v, "children": kids}).encode()
        if suffix == "/nodes/925?f=json":
            return json.dumps(self.leaf("925", 14.4)).encode()
        if suffix == "/nodes/926?f=json":
            return json.dumps(self.leaf("926", 14.43)).encode()
        if suffix.endswith("/geometries/0"):
            return GEO
        raise AssertionError(suffix)


def test_orthometric_to_ellipsoid_adds_positive_undulation(heights, grid):
    assert heights.ellipsoid(14.4, 50.09, 279) == pytest.approx(324, abs=1e-6)
    assert heights.sha == cm.digest(grid.read_bytes())
    with pytest.raises(Exception):
        heights.ellipsoid(0, 0, 279)  # no silent height outside the grid


def test_geometry_keeps_its_absolute_place_on_earth(heights):
    positions, normals, colors, anchor = cm.decode(GEO, {"obb": {"center": CENTER}}, heights)
    np.testing.assert_allclose(anchor, [50.09, 14.4, 324], atol=1e-6)
    np.testing.assert_allclose(positions[0], [0, 0, 0], atol=1e-6)
    assert positions[1, 0] > 7 and abs(positions[1, 2]) < .001
    np.testing.assert_allclose(positions[2], [0, 10, 0], atol=1e-5)
    np.testing.assert_allclose(np.linalg.norm(normals, axis=1), 1, atol=1e-6)
    np.testing.assert_allclose(colors[0], [1, .2158605, 0], atol=1e-6)
    # PROJ's own geodetic -> ECEF, curvature included
    proj = Transformer.from_crs(4979, 4978, always_xy=True)
    np.testing.assert_allclose(cm.ecef(14.4, 50.09, 324), proj.transform(14.4, 50.09, 324), atol=1e-7)


def test_a_glb_is_one_embedded_mesh_with_normals(heights):
    p, n, c, _ = cm.decode(GEO, {"obb": {"center": CENTER}}, heights)
    body = cm.glb(p, n, c)
    magic, version, length, size, kind = struct.unpack_from("<5I", body)
    assert (magic, version, length, kind) == (0x46546c67, 2, len(body), 0x4e4f534a)
    doc = json.loads(body[20:20 + size])
    assert "uri" not in doc["buffers"][0] and doc["accessors"][0]["count"] == 3
    assert doc["meshes"][0]["primitives"][0]["attributes"] == {"POSITION": 0, "NORMAL": 1, "COLOR_0": 2}
    assert struct.unpack_from("<II", body, 20 + size) == (108, 0x004e4942)


def test_malformed_counts_truncation_and_nonfinite_vertices_fail(heights):
    node = {"obb": {"center": CENTER}}
    for bad in [GEO[:7], GEO[:-1], GEO + b"x", struct.pack("<II", 1_000_000, 1) + GEO[8:],
                geometry([[float("nan"), 0, 0], [0, 0, 0], [0, 0, 0]])]:
        with pytest.raises(cm.CityModelError):
            cm.decode(bad, node, heights)


def test_a_missing_grid_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        cm.Heights(tmp_path / "missing.gtx")


def test_kept_source_bytes_are_checked_and_never_refetched(grid, monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "_fetch", lambda url: calls.append(url) or b"original")
    src = cm.Sources(cm.models_dir() / "ipr", False)
    assert src.get("/nodes/925") == b"original" and src.get("/nodes/925") == b"original"
    assert len(calls) == 1
    (cm.models_dir() / "blobs" / cm.digest(b"original")).write_bytes(b"corrupt")
    with pytest.raises(cm.CityModelError, match="checksum"):
        src.get("/nodes/925")

    def offline(url):
        raise cm.CityModelError("offline")
    monkeypatch.setattr(cm, "_fetch", offline)
    with pytest.raises(cm.CityModelError, match="offline"):
        src.get("/nodes/925", current=True)


def test_an_area_is_the_leaves_its_circle_meets_and_is_deterministic(grid, monkeypatch):
    scene = Scene(monkeypatch)
    sha, area = cm.prepare(grid, 50.09, 14.4, 100)
    assert [a["node"] for a in area["assets"]] == ["925"]          # 926 is 2 km east
    a = area["assets"][0]
    assert a["anchor"] == [50.09, 14.4, 324] and a["url"] == f"assets/{a['sha256']}.glb"
    assert (cm.models_dir() / "assets" / f"{a['sha256']}.glb").is_file()
    assert cm.prepare(grid, 50.09, 14.4, 100)[0] == sha
    assert scene.fetched.count("/nodes/925/geometries/0") == 1     # the second time from the kept bytes


def test_a_scene_that_changes_during_preparation_publishes_nothing(grid, monkeypatch):
    scene = Scene(monkeypatch)
    scene.changed = True
    with pytest.raises(cm.CityModelError, match="changed during preparation"):
        cm.prepare(grid, 50.09, 14.4, 100)
    assert not (cm.models_dir() / "areas").exists()


def test_the_area_budget_is_explicit(grid):
    for radius in [0, -1, 2001, float("nan")]:
        with pytest.raises(cm.CityModelError):
            cm.prepare(grid, 50.09, 14.4, radius)


@pytest.fixture
def svc(grid, monkeypatch):
    Scene(monkeypatch)
    west, _ = cm.prepare(grid, 50.09, 14.4, 100)
    east, _ = cm.prepare(grid, 50.09, 14.43, 100)
    assert cli.main(["release", "register", "models", "models-a", "--areas", f"{west},{east}"]) == 0
    s = store.Store()
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), s


def test_a_release_is_its_areas_meshes_once_and_serves_only_those(svc):
    c, s = svc
    r = s.release("models-a")
    assert r.layer == "models" and r.immutable and r.sources["assets"] == 2
    m = c.get("/t/models-a/models/manifest.json")
    assert m.status_code == 200 and "immutable" in m.headers["cache-control"] and m.headers["x-attribution"] == "ipr"
    assert cm.digest(m.content) == r.sources["manifest"]
    manifest = m.json()
    assert [a["node"] for a in manifest["assets"]] == ["925", "926"] and len(manifest["areas"]) == 2
    a = manifest["assets"][0]
    g = c.get(f"/t/models-a/models/{a['url']}")
    assert g.status_code == 200 and g.headers["content-type"] == "model/gltf-binary"
    assert cm.digest(g.content) == a["sha256"]
    other = "0" * 64
    assert c.get(f"/t/models-a/models/assets/{other}.glb").status_code == 404
    assert c.get("/t/nope/models/manifest.json").status_code == 404


def test_a_release_refuses_areas_never_prepared_and_none(grid):
    assert cli.main(["release", "register", "models", "models-b", "--areas", "f" * 64]) == 2
    assert cli.main(["release", "register", "models", "models-b"]) == 2


def test_a_gzipped_answer_is_kept_as_the_resource_itself(monkeypatch):
    import gzip
    import io

    class Answer(io.BytesIO):
        def __init__(self, body, encoding):
            super().__init__(body)
            self.headers = {"Content-Encoding": encoding}

    monkeypatch.setattr(cm.urllib.request, "urlopen", lambda req, timeout: Answer(gzip.compress(b'{"a":1}'), "gzip"))
    assert cm._fetch(cm.BASE + "?f=json") == b'{"a":1}'
    monkeypatch.setattr(cm.urllib.request, "urlopen", lambda req, timeout: Answer(b"x", "br"))
    with pytest.raises(cm.CityModelError, match="Content-Encoding br"):
        cm._fetch(cm.BASE + "?f=json")
