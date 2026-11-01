"""The map: a release is one PMTiles archive in the store's osm/ directory (a tiny one is written
here), its tiles go out as stored, 204 where it has none, 404 past its zooms."""
import gzip
import json
import os

import pytest
from fastapi.testclient import TestClient
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from earth import app as app_mod, cli, limits, osm, store

MVT = gzip.compress(b"\x1a\x05fake!", mtime=0)       # earth never decodes a tile


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    monkeypatch.setattr(osm, "_open", {})
    os.makedirs(osm.osm_dir())
    with open(os.path.join(osm.osm_dir(), "cz-test.pmtiles"), "wb") as f:
        w = Writer(f)
        for z, x, y in [(13, 4429, 2771), (15, 17718, 11086)]:
            w.write_tile(zxy_to_tileid(z, x, y), MVT)
        w.finalize({"tile_type": TileType.MVT, "tile_compression": Compression.GZIP,
                    "min_lon_e7": 140000000, "min_lat_e7": 500000000, "max_lon_e7": 150000000,
                    "max_lat_e7": 510000000, "center_zoom": 14, "center_lon_e7": 145000000,
                    "center_lat_e7": 505000000}, {"name": "test"})
    s = store.Store()
    s.register("osm-a", "osm", osm.describe("cz-test.pmtiles"), "test")
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), s


def test_a_tile_goes_out_as_the_archive_holds_it(svc):
    c, _ = svc
    r = c.get("/t/osm-a/osm/15/17718/11086.mvt", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-type"] == osm.MEDIA_TYPE
    assert r.content == b"\x1a\x05fake!"                  # httpx inflates the gzip it was sent
    assert r.headers["content-encoding"] == "gzip" and r.headers["x-attribution"] == "osm"
    assert r.headers["cache-control"] == app_mod.IMMUTABLE


def test_none_inside_its_zooms_is_empty_ground_and_past_them_is_404(svc):
    c, _ = svc
    assert c.get("/t/osm-a/osm/15/17718/11087.mvt").status_code == 204
    assert c.get("/t/osm-a/osm/16/35436/22172.mvt").status_code == 404
    assert c.get("/t/osm-a/osm/12/2214/1385.mvt").status_code == 404
    assert c.get("/t/nope/osm/15/17718/11086.mvt").status_code == 404


def test_a_release_records_the_archive_and_the_attribution_is_odbl(svc):
    c, s = svc
    src = s.release("osm-a").sources
    assert src["pmtiles"] == "cz-test.pmtiles" and len(src["sha256"]) == 64 and src["zooms"] == [13, 15]
    assert c.get("/attribution").json()["sources"]["osm"]["licence"] == "ODbL 1.0"
    with pytest.raises(osm.OsmError, match="copy the .pmtiles there"):
        osm.describe("../elsewhere.pmtiles")


def test_the_cli_registers_an_archive_frozen(svc, capsys):
    assert cli.main(["release", "register", "osm", "osm-b", "--pmtiles", "cz-test.pmtiles"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "frozen"
    assert cli.main(["release", "register", "osm", "osm-c", "--live", "--pmtiles", "cz-test.pmtiles"]) == 2
