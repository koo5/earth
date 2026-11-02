"""Names: the workbench's query and parser over an extract's named points, heights made ellipsoidal (a missing ele
from the surface), one frozen file per release, and a disc round a point answered as /terrain/labels answers it."""
import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from earth import app as app_mod, cli, limits, names, store


def node(lon, lat, **tags):
    return "\x1e" + json.dumps({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                                "properties": tags})


LINES = [
    node(14.40, 50.10, natural="peak", name="Petřín", ele="327 m", prominence="1,602"),
    node(14.41, 50.10, man_made="tower", **{"tower:type": "observation"}, name="Rozhledna"),
    node(14.42, 50.10, man_made="mast", **{"tower:type": "communication"}, name="Vysílač", ele="400"),
    node(14.43, 50.10, man_made="tower", **{"tower:type": "water"}, name="Vodojem"),    # a water tower, its own kind
    node(14.435, 50.10, man_made="water_tower", name="Vodárna", ele="250"),
    node(14.44, 50.10, man_made="tower", name="Věž"),                                   # no type: not either
    node(14.45, 50.10, place="town", name="Město", population="1 324 277"),
    node(14.46, 50.10, place="hamlet", name="Samota"),                                  # not a kind it asks for
    node(14.47, 50.10, natural="peak"),                                                 # nameless
    json.dumps({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[14, 50], [14.1, 50]]},
                "properties": {"natural": "peak", "name": "Way"}}),
    "",
]


def test_the_workbench_query_and_parser_read_an_extract():
    f = {r["name"]: r for r in names.features(LINES)}
    assert sorted(f) == ["Město", "Petřín", "Rozhledna", "Vodojem", "Vodárna", "Vysílač"]
    assert f["Vodojem"]["kind"] == f["Vodárna"]["kind"] == "water_tower" and f["Vodárna"]["ele"] == 250.0
    assert f["Petřín"] == {"name": "Petřín", "lat": 50.1, "lon": 14.4, "kind": "peak", "ele": 327.0, "prominence": 1.602}
    assert f["Rozhledna"]["kind"] == "tower" and f["Rozhledna"]["ele"] is None
    assert f["Vysílač"]["kind"] == "mast" and f["Vysílač"]["ele"] == 400.0
    assert f["Město"]["kind"] == "town" and f["Město"]["population"] == 1_324_277


def test_heights_are_ellipsoidal_a_missing_ele_comes_from_the_surface_and_no_height_is_no_name():
    feats = [{"name": "a", "kind": "peak", "lat": 50.0, "lon": 14.0, "ele": 300.0},
             {"name": "b", "kind": "tower", "lat": 50.0, "lon": 14.1, "ele": None},
             {"name": "c", "kind": "town", "lat": 50.0, "lon": 14.2, "ele": None}]
    asked = []

    def surface(lons, lats):
        asked.append(list(lons))
        return np.array([250.0, np.nan]), np.array([True, False])

    out = names.with_heights(feats, surface, lambda lats, lons: np.full(len(lats), 45.0))
    assert asked == [[14.1, 14.2]]                       # only the missing ones are sampled
    assert [(r["name"], r["ele"], r["h"], r.get("ele_estimated")) for r in out] == [
        ("a", 300.0, 345.0, None), ("b", 250.0, 295.0, True)]


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    names._load.cache_clear()
    os.makedirs(names.names_dir())
    src = tmp_path / "cz-names.geojsonseq"
    # a ring of peaks 1..9 km east of Prague's centre, higher further out, and a town 150 km away
    src.write_text("\n".join([node(14.42 + k * 0.014, 50.08, natural="peak", name=f"p{k}", ele=str(200 + k))
                              for k in range(1, 10)] + [node(16.5, 50.08, place="town", name="far", ele="300")]),
                   encoding="utf-8")
    summary = names.build(str(src), os.path.join(names.names_dir(), "names-cz.json"),
                          surface=lambda lons, lats: (np.full(len(lons), np.nan), np.zeros(len(lons), bool)),
                          geoid=lambda lats, lons: np.full(len(lats), 45.0))
    return summary


def test_a_disc_answers_near_first_chunks_highest_first(built):
    assert built == {"read": 10, "kept": 10, "estimated": 0, "kinds": {"peak": 9, "town": 1}}
    n = names.release("names-cz.json")
    all_ = n.disc(50.08, 14.42, 120_000)["labels"]
    assert [r["name"] for r in all_] == [f"p{k}" for k in range(9, 0, -1)]      # "far" is past 120 km
    chunks = [n.disc(50.08, 14.42, 120_000, c, 3) for c in range(3)]
    assert [[r["name"] for r in ch["labels"]] for ch in chunks] == [["p3", "p2", "p1"], ["p6", "p5", "p4"],
                                                                     ["p9", "p8", "p7"]]
    assert chunks[1]["chunk"] == 1 and chunks[1]["chunks"] == 3
    assert n.disc(50.08, 14.42, 300_000)["labels"][0]["name"] == "far"            # 200 km at most... and it is 150
    with pytest.raises(names.NamesError):
        n.disc(95, 14, 1000)


def test_a_release_is_one_frozen_file_served_as_the_labels_api_answers(built):
    assert cli.main(["release", "register", "names", "names-cz", "--names", "names-cz.json"]) == 0
    assert cli.main(["release", "register", "names", "names-x", "--names", "missing.json"]) == 2
    s = store.Store()
    r = s.release("names-cz")
    assert r.layer == "names" and r.immutable and r.sources["count"] == 10 and len(r.sources["sha256"]) == 64
    c = TestClient(app_mod.create_app(store=s, limits=limits.Limits()))
    got = c.get("/t/names-cz/names", params={"lat": 50.08, "lon": 14.42, "radius_m": 3500, "chunk": 0, "chunks": 1})
    assert got.status_code == 200 and got.headers["x-attribution"] == "osm"
    assert got.headers["cache-control"] == app_mod.IMMUTABLE
    body = got.json()
    assert [x["name"] for x in body["labels"]] == ["p3", "p2", "p1"] and body["chunks"] == 1
    assert body["labels"][0] == {"name": "p3", "lat": 50.08, "lon": 14.462, "kind": "peak", "ele": 203.0, "h": 248.0}
    assert c.get("/t/nope/names", params={"lat": 50, "lon": 14}).status_code == 404
    assert c.get("/t/names-cz/names", params={"lat": 95, "lon": 14}).status_code == 422
