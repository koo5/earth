"""The orthophoto: asked of the ortho service every time (faked here at ortho.fetch), never kept, what it
cost charged to the make bucket, 404 outside Czechia without asking, and its attribution with the year."""
import pytest
from fastapi.testclient import TestClient

from earth import app as app_mod, limits, ortho, store
from conftest import tile_of

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
COST: dict = {}                                                # what the ortho service says a tile cost
CHURCH = tile_of(50.18723, 14.661822, 18)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    asked = []

    def fetch(z, x, y):
        asked.append((z, x, y))
        return None if (z, x, y) == (18, 1, 1) else (JPEG, "image/jpeg", COST.get((z, x, y), 0))
    monkeypatch.setattr(ortho, "fetch", fetch)
    s = store.Store()
    s.register("ortho-a", "ortho", {"year": "2024-2025"}, "test", mode="live")
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), asked


def test_a_tile_is_passed_on_as_it_came_and_never_kept(svc):
    c, asked = svc
    z, x, y = CHURCH
    for _ in range(2):
        r = c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg")
        assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
        assert r.headers["x-attribution"] == "cuzk-ortho" and r.headers["cache-control"] == app_mod.LIVE
    assert asked == [CHURCH, CHURCH]                               # the ortho service caches, not earth


def test_what_a_tile_cost_is_charged_and_an_address_in_debt_waits(tmp_path, monkeypatch):
    monkeypatch.delenv("EARTH_NO_LIMITS", raising=False)
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("EARTH_LIMIT_MAKE_RATE", "0.001")
    monkeypatch.setenv("EARTH_LIMIT_MAKE_BURST", "3")
    cheap, dear = tile_of(50.1, 14.4, 17), tile_of(50.2, 14.5, 17)
    monkeypatch.setattr(ortho, "fetch", lambda z, x, y: (JPEG, "image/jpeg", 5 if (z, x, y) == dear else 0))
    s = store.Store()
    s.register("ortho-a", "ortho", {"year": "2024-2025"}, "test", mode="live")
    c = TestClient(app_mod.create_app(store=s, limits=limits.Limits()))
    for _ in range(10):                                            # cached tiles cost nothing made
        assert c.get("/t/ortho-a/ortho/{}/{}/{}.jpg".format(*cheap)).status_code == 200
    assert c.get("/t/ortho-a/ortho/{}/{}/{}.jpg".format(*dear)).status_code == 200    # 3 - 5: in debt
    r = c.get("/t/ortho-a/ortho/{}/{}/{}.jpg".format(*cheap))
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 1


def test_outside_czechia_and_where_cuzk_has_none_is_404_and_not_kept(svc):
    c, asked = svc
    z, x, y = tile_of(48.85, 2.35, 18)                         # Paris: never asked upstream
    assert c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg").status_code == 404 and asked == []
    assert c.get("/t/ortho-a/ortho/18/1/1.jpg").status_code == 404     # wholly outside too
    assert asked == []
    assert c.get("/t/nope/ortho/18/1/1.jpg").status_code == 404
    assert c.get(f"/t/ortho-a/ortho/{ortho.MAX_Z + 1}/0/0.jpg").status_code == 404


def test_the_attribution_names_cuzk_with_the_terms_and_the_metadata(svc):
    c, _ = svc
    a = c.get("/attribution").json()["sources"]["cuzk-ortho"]
    assert a["text"] == "ČÚZK, {year}" and a["licence"] == "CC BY 4.0"
    assert "Podminky" in a["terms_url"] and "CZ-CUZK-ORTOFOTO-R" in a["metadata_url"]
    assert c.get("/releases/ortho-a").json()["sources"]["year"] == "2024-2025"


def test_coverage_is_the_service_extent():
    assert ortho.covered(*tile_of(50.08, 14.42, 15)) and ortho.covered(*tile_of(49.0, 17.0, 12))
    assert not ortho.covered(*tile_of(48.85, 2.35, 10))
