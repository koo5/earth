"""Command-construction tests for build_mosaic.py — no pdal/gdal needed."""
import math
from pathlib import Path

import pytest

from build_mosaic import cog_cmd, pdal_pipeline, vrt_cmd, warp_cmd


def test_pdal_pipeline_kinds():
    dsm = pdal_pipeline(Path("a.laz"), Path("a.tif"), 2.0, "dsm")["pipeline"][1]
    dtm = pdal_pipeline(Path("a.laz"), Path("a.tif"), 2.0, "dtm")["pipeline"][1]
    assert dsm["output_type"] == "max"      # ridge/canopy line preserved
    assert dtm["output_type"] == "idw"
    assert dsm["resolution"] == 2.0 and dsm["nodata"] == -9999


def test_warp_cmd_square_metric_cells():
    cmd = warp_cmd(Path("in.tif"), Path("out.tif"), 30.0, 49.8,
                   "EPSG:5514+8357", "max")
    i = cmd.index("-tr")
    dlon, dlat = float(cmd[i + 1]), float(cmd[i + 2])
    assert dlat * 111_320 == pytest.approx(30.0, rel=1e-6)
    assert dlon * 111_320 * math.cos(math.radians(49.8)) == pytest.approx(30.0, rel=1e-4)
    assert cmd[cmd.index("-t_srs") + 1] == "EPSG:4326"
    assert cmd[cmd.index("-s_srs") + 1] == "EPSG:5514+8357"
    assert cmd[cmd.index("-r") + 1] == "max"
    assert "-novshift" in cmd     # heights stay Bpv: GDAL 3.13 + PROJ network made them ellipsoidal


def test_warp_cmd_no_s_srs_by_default():
    cmd = warp_cmd(Path("in.tif"), Path("out.tif"), 30.0, 49.8, None, "bilinear")
    assert "-s_srs" not in cmd


def test_vrt_and_cog_cmds():
    cmd = vrt_cmd([Path("a.tif"), Path("b.tif")], Path("m.vrt"))
    assert cmd[:3] == ["gdalbuildvrt", "-resolution", "highest"]
    assert cmd[-2:] == ["a.tif", "b.tif"]
    assert cog_cmd(Path("m.vrt"), Path("m.tif"))[cog_cmd(Path("m.vrt"), Path("m.tif")).index("-of") + 1] == "COG"


# --- whole sheets only, several at once (all of Czechia is built) -----------------------------------------

import json  # noqa: E402
import threading  # noqa: E402

import build_mosaic  # noqa: E402


def fake_tools(monkeypatch, fail=()):
    """run() writes the output its command names (pdal: the pipeline's filename; gdalwarp: the last
    argument) instead of running pdal/gdal; a source named in `fail` raises before writing."""
    seen, lock = [], threading.Lock()

    def run(cmd, dry_run):
        if cmd[0] == "pdal":
            out = Path(json.loads(Path(cmd[2]).read_text())["pipeline"][1]["filename"])
            src = Path(json.loads(Path(cmd[2]).read_text())["pipeline"][0]["filename"])
        else:
            out, src = Path(cmd[-1]), Path(cmd[-2])
        with lock:
            seen.append((src.name, out.name))
        if src.stem in fail:
            raise RuntimeError(f"{src.name} failed")
        out.write_bytes(b"tif")

    monkeypatch.setattr(build_mosaic, "run", run)
    return seen


def sheets(tmp_path, names, ext):
    d = tmp_path / "in"
    d.mkdir()
    for n in names:
        (d / f"{n}{ext}").write_bytes(b"x")
    return d


def test_rasterize_writes_through_part_and_skips_whole_sheets(tmp_path, monkeypatch):
    seen = fake_tools(monkeypatch)
    laz = sheets(tmp_path, ["A", "B", "C"], ".laz")
    out = tmp_path / "rast"
    out.mkdir()
    (out / "A.tif").write_bytes(b"done before")          # a whole sheet: kept
    (out / "B.tif.part").write_bytes(b"half")            # an interrupted one: rebuilt
    build_mosaic.main(["rasterize", "--laz-dir", str(laz), "--out-dir", str(out), "--kind", "dsm", "--jobs", "2"])
    assert sorted(seen) == [("B.laz", "B.tif.part"), ("C.laz", "C.tif.part")]
    assert (out / "A.tif").read_bytes() == b"done before"
    assert sorted(p.name for p in out.glob("*.tif*")) == ["A.tif", "B.tif", "C.tif"]


def test_warp_in_parallel_does_every_sheet_and_never_lists_a_part(tmp_path, monkeypatch):
    seen = fake_tools(monkeypatch)
    src = sheets(tmp_path, [f"S{i:02d}" for i in range(12)], ".tif")
    (src / "half.tif.part").write_bytes(b"x")            # an unfinished raster is not an input
    out = tmp_path / "warp"
    build_mosaic.main(["warp", "--in-dir", str(src), "--out-dir", str(out), "--res-m", "10", "--jobs", "5"])
    assert len(seen) == 12 and not any(s.startswith("half") for s, _ in seen)
    assert len(list(out.glob("*.tif"))) == 12 and not list(out.glob("*.part"))


def test_a_failed_sheet_leaves_no_output_and_fails_the_step_after_the_rest(tmp_path, monkeypatch):
    seen = fake_tools(monkeypatch, fail={"S03"})
    src = sheets(tmp_path, [f"S{i:02d}" for i in range(6)], ".tif")
    out = tmp_path / "warp"
    with pytest.raises(RuntimeError, match="S03"):
        build_mosaic.main(["warp", "--in-dir", str(src), "--out-dir", str(out), "--res-m", "10", "--jobs", "3"])
    assert len(seen) == 6                                 # the others still ran
    assert sorted(p.name for p in out.glob("*.tif")) == [f"S{i:02d}.tif" for i in range(6) if i != 3]


def test_warp_names_its_format():
    cmd = warp_cmd(Path("in.tif"), Path("out.tif.part"), 10.0, 49.8, None, "bilinear")
    assert cmd[cmd.index("-of") + 1] == "GTiff"
