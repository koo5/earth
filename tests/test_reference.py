"""reference.check and `earth release register terrain` against the synthetic world (conftest.py): the
mosaics are checked at surveyed summits before a release is registered."""
import json

import pytest

from conftest import A, CELL, make_tif, make_vrt, plane
from earth import cli, reference, store

# three summits on the built sheet, on cell centres away from its hole, their surveyed height the plane's
SUMMITS = [{"name": n, "lon": A[0] + (i + 0.5) * CELL, "lat": A[1] + (j + 0.5) * CELL,
            "ele": round(float(plane(A[0] + (i + 0.5) * CELL, A[1] + (j + 0.5) * CELL)), 3)}
           for n, i, j in (("Alpha", 12, 3), ("Beta", 25, 9), ("Gamma", 33, 15))]


def mosaic(world, name, fn):
    make_tif(str(world / f"warp/{name}/AAAA00.tif"), A, fn)
    make_vrt(str(world / f"{name}.vrt"), [(f"warp/{name}/AAAA00.tif", A)], A)


@pytest.fixture
def built(world, tmp_path, monkeypatch):
    """All four ČÚZK mosaics built, the surface open ground (= the bare one), and the summits as the reference."""
    for name in ("dtm10", "dtm2", "dsm10", "dsm2"):
        mosaic(world, name, plane)
    f = tmp_path / "reference_heights.json"
    f.write_text(json.dumps({"summits": SUMMITS}))
    monkeypatch.setattr(reference, "REFERENCE_FILE", str(f))
    return world


def test_a_build_on_its_surveyed_heights_passes(built):
    got = reference.check()
    assert set(got) == set(reference.MOSAICS)
    assert all(m["points"] == 3 and m["max_abs_m"] < 0.01 for m in got.values())


def test_an_ellipsoidal_mosaic_is_refused_naming_it_and_its_summits(built):
    mosaic(built, "dsm2", lambda lo, la: plane(lo, la) + 44.0)
    with pytest.raises(reference.ReferenceError, match=r"3 reference summit readings past 5 m") as e:
        reference.check()
    assert "dsm2.vrt at Alpha" in str(e.value) and "dtm10.vrt" not in str(e.value)


def test_one_changed_summit_is_refused_too(built):
    s = [dict(p) for p in SUMMITS]
    s[1]["ele"] += 6.0
    with pytest.raises(reference.ReferenceError, match="at Beta") as e:
        reference.check(s)
    assert "Alpha" not in str(e.value)


def test_a_mosaic_with_no_summit_on_it_is_not_a_pass(built):
    (built / "dtm2.vrt").unlink()
    with pytest.raises(reference.ReferenceError, match=r"no reference summit on dtm2\.vrt"):
        reference.check()


def test_the_cli_registers_only_a_checked_release_or_one_that_says_why(built, capsys):
    assert cli.main(["release", "register", "terrain", "terrain-a"]) == 0
    r = store.Store().release("terrain-a")
    assert r.sources["reference"]["bare_vrt"]["points"] == 3
    mosaic(built, "dtm10", lambda lo, la: plane(lo, la) + 44.0)
    capsys.readouterr()
    assert cli.main(["release", "register", "terrain", "terrain-b"]) == 2
    assert "not registered" in capsys.readouterr().err and store.Store().release("terrain-b") is None
    assert cli.main(["release", "register", "terrain", "terrain-b", "--unchecked", " "]) == 2
    assert cli.main(["release", "register", "terrain", "terrain-b", "--unchecked", "a test bbox"]) == 0
    assert store.Store().release("terrain-b").sources["reference"] == {"unchecked": "a test bbox"}
    assert cli.main(["release", "register", "osm", "osm-x", "--pmtiles", "x.pmtiles", "--unchecked", "y"]) == 2


def test_the_shipped_summits_are_in_czechia_with_heights():
    s = reference.load()
    assert len(s) >= 20 and len({p["name"] for p in s}) == len(s)
    assert all(48.5 < p["lat"] < 51.1 and 12.0 < p["lon"] < 18.9 and 100 < p["ele"] < 1610 for p in s)


def test_an_unreadable_mosaic_is_not_registered_either(built, capsys):
    (built / "dsm2.vrt").write_text("not a raster")
    assert cli.main(["release", "register", "terrain", "terrain-c"]) == 2
    assert "not registered" in capsys.readouterr().err and store.Store().release("terrain-c") is None
