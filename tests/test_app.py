"""The service end to end: a channel names a release, the release serves tiles, kept tiles are
immutable, incomplete ones are not kept, a frozen release refuses new data, limits answer 429."""
import gzip
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from earth import app as app_mod, limits, store, tiles
from conftest import GEOID_N, plane, tile_of

BUILT = tile_of(50.01, 14.02, 15)
UNBUILT = tile_of(50.01, 14.06, 15)


@pytest.fixture
def svc(world, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    s = store.Store()
    s.register("terrain-a", "terrain", {m: tiles.source_stamp(m) for m in tiles.MODELS}, "test")
    s.set_channel("prod", {"terrain": "terrain-a"})
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), s


def url(z, x, y, model="bare", fmt="q16", rid="terrain-a"):
    return f"/t/{rid}/terrain/{model}/{z}/{x}/{y}.{fmt}"


def test_a_channel_names_the_release_whose_tiles_are_kept_and_immutable(svc):
    c, _ = svc
    assert c.get("/channels/prod").json()["releases"] == {"terrain": "terrain-a"}
    assert c.get("/channels/nope").status_code == 404
    r = c.get(url(*BUILT))
    assert r.status_code == 200, r.text
    assert r.headers["x-tile-complete"] == "1" and r.headers["x-tile-kept"] == "0"
    assert r.headers["x-tile-format"] == "q16" and r.headers["x-tile-heights"] == "ellipsoidal"
    assert r.headers["cache-control"] == app_mod.IMMUTABLE
    # ČÚZK, ETOPO under the one nodata cell of the sheet if it falls inside, EGM2008 always
    used = r.headers["x-tile-sources"]
    assert r.headers["x-attribution"] == ("cuzk,etopo,egm2008" if "etopo60" in used else "cuzk,egm2008")
    h, src = tiles.decode_q16(r.content)
    assert np.nanmin(h) > 100 + GEOID_N - 1
    again = c.get(url(*BUILT))
    assert again.headers["x-tile-kept"] == "1" and again.content == r.content
    # the browser asks gzipped; the kept body goes out as it is stored
    raw = c.get(url(*BUILT), headers={"Accept-Encoding": "gzip"})
    assert raw.content == r.content                   # httpx inflates it; the bytes are the same tile
    f32 = c.get(url(*BUILT, fmt="f32"))
    assert len(f32.content) == 5 * 65 * 65


def test_an_incomplete_tile_is_answered_but_not_kept(svc):
    c, s = svc
    r = c.get(url(*UNBUILT))
    assert r.headers["x-tile-complete"] == "0" and r.headers["x-tile-waiting"] == "cuzk"
    assert r.headers["cache-control"] == "no-store"
    assert c.get(url(*UNBUILT)).headers["x-tile-kept"] == "0"


def test_bad_requests_are_404(svc):
    c, _ = svc
    assert c.get(url(*BUILT, rid="nope")).status_code == 404
    assert c.get(url(*BUILT, model="dsm")).status_code == 404
    assert c.get(url(*BUILT, fmt="png")).status_code == 404
    assert c.get(url(30, 0, 0)).status_code == 404


def test_a_frozen_release_serves_what_it_has_and_makes_nothing_from_other_data(svc, world):
    c, s = svc
    assert c.get(url(*BUILT)).status_code == 200
    # the mosaic is rebuilt (a sheet added): the release's stamp no longer matches
    st = os.stat(world / "dtm10.vrt")
    os.utime(world / "dtm10.vrt", (st.st_atime, st.st_mtime + 100))
    assert c.get(url(*BUILT)).headers["x-tile-kept"] == "1"
    r = c.get(url(*tile_of(50.015, 14.03, 16)))
    assert r.status_code == 409 and "new release" in r.text
    # a live release follows the mosaics, and is never immutable
    s.register("terrain-live", "terrain", {}, "test", mode="live")
    live = c.get(url(*BUILT, rid="terrain-live"))
    assert live.status_code == 200 and live.headers["cache-control"] == app_mod.LIVE


def test_limits_answer_429_with_retry_after(world, monkeypatch):
    monkeypatch.delenv("EARTH_NO_LIMITS", raising=False)
    monkeypatch.setenv("EARTH_LIMIT_MAKE_RATE", "0.01")
    monkeypatch.setenv("EARTH_LIMIT_MAKE_BURST", "1")
    s = store.Store()
    s.register("terrain-a", "terrain", {m: tiles.source_stamp(m) for m in tiles.MODELS}, "test")
    c = TestClient(app_mod.create_app(store=s, limits=limits.Limits()))
    assert c.get(url(*BUILT)).status_code == 200
    assert c.get(url(*BUILT)).status_code == 200             # kept: a read, not a make
    r = c.get(url(*tile_of(50.015, 14.03, 16)))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_attribution_and_cors(svc):
    c, _ = svc
    a = c.get("/attribution").json()["sources"]
    assert {"cuzk", "glo30", "etopo", "egm2008"} <= set(a)
    assert "liability" in a["glo30"]["terms"]
    assert a["glo30"]["text"].startswith("produced using Copernicus WorldDEM-30 ©")
    r = c.get(url(*BUILT), headers={"Origin": "https://hillview.cz"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "X-Tile-Sources" in r.headers["access-control-expose-headers"]


def test_ground_answers_points_and_boxes_with_a_status_each(svc):
    c, _ = svc
    g = "/t/terrain-a/terrain/ground"
    r = c.post(g, json={"points": [[14.0105, 50.0105], [14.06, 50.01], [14.5, 49.0]], "want": "bare"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == ["ok", "not_fetched", "outside"] and j["vertical"] == "orthometric"
    assert abs(j["heights"][0] - plane(14.0105, 50.0105)) < 0.01        # the model's own height
    assert j["heights"][1:] == [None, None] and r.headers["cache-control"] == "no-store"
    box = c.post(g, json={"bbox": [14.01, 50.005, 14.012, 50.007], "step_m": 50}).json()
    assert len(box["points"]) == len(box["heights"]) > 1 and box["counts"]["ok"] == len(box["points"])
    assert c.post(g, json={"bbox": [14.0, 50.0, 14.1, 50.1], "step_m": 1}).status_code == 422
    assert c.post(g, json={"points": [[14.0, 50.0]], "bbox": [0, 0, 1, 1]}).status_code == 422
    assert c.post("/t/nope/terrain/ground", json={"points": [[14.0, 50.0]]}).status_code == 404


def test_a_frozen_release_answers_ground_only_from_its_own_mosaics(svc, world):
    c, _ = svc
    st_ = os.stat(world / "dtm10.vrt")
    os.utime(world / "dtm10.vrt", (st_.st_atime, st_.st_mtime + 100))
    r = c.post("/t/terrain-a/terrain/ground", json={"points": [[14.0105, 50.0105]]})
    assert r.status_code == 409 and "newer release" in r.text
