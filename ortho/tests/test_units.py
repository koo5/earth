"""On-demand sheets, the overview's hysteresis and the cache paths, without the container.

    uv run --with pytest --with pyproj --with pillow pytest tests/test_units.py
"""
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import overview  # noqa: E402
import sheets as S  # noqa: E402
from store import SheetMissing, Store  # noqa: E402

KNOWN = S.load()


# --- which sheets a tile needs --------------------------------------------------------------------------

def test_the_list_is_all_of_czechia():
    assert len(KNOWN) == 20308 and "602_5554" in KNOWN


def test_a_sheet_is_its_2km_square():
    assert S.sheet_box("602_5554") == (602000, 5554000, 604000, 5556000)


def test_a_box_over_a_corner_needs_the_four_sheets_round_it():
    assert S.ids_in_box((603990, 5555990, 604010, 5556010), KNOWN) == ["602_5554", "602_5556", "604_5554", "604_5556"]


def test_the_margin_takes_the_neighbour_a_resample_would_read():
    inside = (602500, 5554500, 603999, 5555500)          # 1 m short of the eastern edge
    assert S.ids_in_box(inside, KNOWN) == ["602_5554", "604_5554"]
    assert S.ids_in_box(inside, KNOWN, margin=0) == ["602_5554"]


def test_sheets_that_do_not_exist_are_never_asked_for():
    assert S.ids_in_box((0, 0, 10, 10), KNOWN) == []


def test_a_zoom15_tile_over_a_sheet_needs_that_sheet():
    from pyproj import Transformer
    lon, lat = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True).transform(603000, 5555000)
    import math
    n = 2 ** 15
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    assert "602_5554" in S.ids_for_tile(15, x, y, KNOWN)


def test_the_crossover_splits_between_zooms_14_and_15():
    assert S.detail_zoom(16000) == 15


def test_a_crossover_inside_a_zoom_is_refused():
    lo, hi = min(S.scale_denominators(15)), max(S.scale_denominators(15))
    with pytest.raises(ValueError, match="splits zoom 15"):
        S.detail_zoom((lo + hi) / 2)


# --- the overview's hysteresis --------------------------------------------------------------------------

def test_nothing_waiting_is_never_due():
    assert not overview.due([], 1e9, 8, 300, 3600)


def test_a_few_fresh_sheets_wait():
    now = 10_000.0
    assert not overview.due([("a", now - 400), ("b", now - 350)], now, 8, 300, 3600)


def test_enough_sheets_wait_for_the_quiet_period():
    now = 10_000.0
    burst = [(f"s{i}", now - 400 + i) for i in range(8)]
    assert overview.due(burst, now, 8, 300, 3600)
    still_arriving = burst + [("late", now - 10)]
    assert not overview.due(still_arriving, now, 8, 300, 3600)


def test_a_trickle_gets_in_after_the_max_age():
    now = 10_000.0
    assert overview.due([("old", now - 3601), ("new", now - 1)], now, 8, 300, 3600)


# --- the tiles an overview update makes stale -----------------------------------------------------------

def test_cached_tiles_are_the_overview_zooms_over_the_sheet_in_tms_rows(tmp_path):
    paths = overview.cached_tiles(str(tmp_path), ["602_5554"], 15)
    zooms = {int(p.relative_to(tmp_path).parts[0]) for p in paths}
    assert zooms == set(range(15))
    assert tmp_path / "0" / "0" / "0.jpeg" in paths and tmp_path / "0" / "0" / "0.png" in paths
    # zoom 1: Czechia is in the north-eastern quarter; tms rows count from the south, so row 1
    assert {p for p in paths if p.parts[-3] == "1"} == {tmp_path / "1" / "1" / f"1.{e}" for e in ("jpeg", "png")}


# --- the store ------------------------------------------------------------------------------------------

def sheet_zip(src: Path, sid: str, missing: str = "") -> None:
    src.mkdir(exist_ok=True)
    with zipfile.ZipFile(src / f"{sid}.zip", "w") as z:
        for ext, body in ((".jp2", b"jp2" * 1000), (".j2w", b"0.125\n"), (".gml", b"<gml/>")):
            if ext != missing:
                z.writestr(f"{sid}{ext}", body)


@pytest.fixture
def store(tmp_path):
    src = tmp_path / "src"
    for sid in ("602_5554", "604_5554", "602_5556"):
        sheet_zip(src, sid)
    return Store(str(tmp_path / "data"), str(tmp_path / "state"), f"file://{src}", fetch_jobs=2)


def test_holding_fetches_what_is_missing_then_indexes_it(store):
    with store.holding(["602_5554", "604_5554"]):
        assert store.jp2("602_5554").exists() and store.jp2("604_5554").exists()
        index = store.index.read_text().splitlines()
    assert index[0] == "location,WKT"
    assert index[1] == (f'{store.jp2("602_5554")},"POLYGON((602000 5554000,604000 5554000,604000 5556000,'
                        '602000 5556000,602000 5554000))"')
    assert len(index) == 3
    assert sorted(sid for sid, _ in store.pending_overview()) == ["602_5554", "604_5554"]
    assert not list((store.data / ".tmp").iterdir())


def test_a_sheet_that_cannot_be_fetched_is_an_error_and_leaves_nothing(store, tmp_path):
    sheet_zip(tmp_path / "src", "606_5554", missing=".j2w")
    with pytest.raises(SheetMissing, match="606_5554"):
        with store.holding(["606_5554"]):
            pytest.fail("a render must not run with a sheet missing")
    assert not store.jp2("606_5554").exists() and not list((store.data / ".tmp").iterdir())
    with pytest.raises(SheetMissing):
        with store.holding(["608_5554"]):                 # not on the server at all
            pass


def test_two_requests_fetch_a_sheet_once(store):
    fetched = []
    original = store._fetch
    store._fetch = lambda sid: (fetched.append(sid) or True) if original(sid) else False
    threads = [threading.Thread(target=lambda: store.holding(["602_5554"]).__enter__()) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fetched == ["602_5554"]


def test_eviction_keeps_what_the_overview_lacks_and_stops_at_the_low_mark(store):
    with store.holding(["602_5554", "604_5554", "602_5556"]):
        pass
    size = store.held_bytes() // 3
    store.mark_overview(["602_5554", "604_5554"])           # 602_5556 still waits for the overview
    with store._db() as con:                                # least recently used first: 604, then 602
        con.execute("UPDATE sheets SET last_used = 1 WHERE id = '604_5554'")
        con.execute("UPDATE sheets SET last_used = 2 WHERE id = '602_5554'")
    assert store.evict(high=3 * size, low=0) == []          # at the budget, not past it
    assert store.evict(high=3 * size - 1, low=2 * size) == ["604_5554"]
    assert not store.jp2("604_5554").exists() and "604_5554" not in store.index.read_text()
    assert store.evict(high=0, low=0) == ["602_5554"]       # 602_5556 is never evicted before the overview
    assert store.jp2("602_5556").exists()


def test_eviction_waits_for_a_render_holding_the_sheet(store):
    with store.holding(["602_5554"]):
        pass
    store.mark_overview(["602_5554"])
    done = []
    with store.holding(["602_5554"]):
        t = threading.Thread(target=lambda: done.append(store.evict(high=0, low=0)))
        t.start()
        time.sleep(0.3)
        assert not done and store.jp2("602_5554").exists()   # the render still has its sheet
    t.join(5)
    assert done == [["602_5554"]] and not store.jp2("602_5554").exists()


def test_reconcile_follows_the_disk(store):
    with store.holding(["602_5554", "604_5554"]):
        pass
    store.jp2("604_5554").unlink()                           # gone behind the record's back
    (store.data / "602_5556.jp2").write_bytes(b"x")          # there without a record
    (store.data / ".tmp" / "half").mkdir()                    # an interrupted fetch
    store.reconcile(KNOWN)
    with store._db() as con:
        assert sorted(r[0] for r in con.execute("SELECT id FROM sheets")) == ["602_5554", "602_5556"]
    assert "604_5554" not in store.index.read_text() and not list((store.data / ".tmp").iterdir())


# --- every sheet opaque, white included --------------------------------------------------------------

def test_white_is_imagery_not_nodata(monkeypatch):
    template = (Path(__file__).resolve().parent.parent / "app" / "templates" / "oi.map.tmpl").read_text()
    assert "OFFSITE" not in template                       # the detail layer keys no colour out
    runs = []
    monkeypatch.setattr(overview, "_run_logged", lambda cmd, label, **kw: runs.append(cmd))

    class Cfg:
        ov_threads, overview = 2, "/state/overview.tif"
    overview.update(Cfg, ["/data/602_5554.jp2"])
    warp = runs[0]
    assert warp[0] == "gdalwarp" and "-srcnodata" not in warp and "-dstalpha" in warp
