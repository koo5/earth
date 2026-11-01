"""The orthophoto: fetched from ČÚZK on first ask (faked here at ortho.fetch), kept as it came,
404 outside Czechia without asking, and its attribution with the release's year."""
import pytest
from fastapi.testclient import TestClient

from earth import app as app_mod, limits, ortho, store
from conftest import tile_of

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
CHURCH = tile_of(50.18723, 14.661822, 18)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    asked = []

    def fetch(z, x, y):
        asked.append((z, x, y))
        return None if (z, x, y) == (18, 1, 1) else (JPEG, "image/jpeg")
    monkeypatch.setattr(ortho, "fetch", fetch)
    s = store.Store()
    s.register("ortho-a", "ortho", {"year": "2024-2025"}, "test", mode="live")
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), asked


def test_a_tile_is_fetched_once_and_kept_as_it_came(svc):
    c, asked = svc
    z, x, y = CHURCH
    r = c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg")
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-tile-kept"] == "0" and r.headers["x-attribution"] == "cuzk-ortho"
    again = c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg")
    assert again.headers["x-tile-kept"] == "1" and again.content == JPEG and asked == [CHURCH]
    assert again.headers["cache-control"] == app_mod.LIVE


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
