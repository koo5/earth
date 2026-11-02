"""On-demand sheets, the overview's hysteresis and the cache paths, without the container.

    uv run --with pytest --with pyproj --with pillow pytest tests/test_units.py
"""
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import overview  # noqa: E402
import serve  # noqa: E402
import sheets as S  # noqa: E402
from store import SheetMissing, Store, StoreFull  # noqa: E402

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

def test_a_cached_tile_is_where_mapproxy_keeps_it(tmp_path):
    # as found on the server: tile 16/35436/22173 at cache/oi/16/35436/22173.mixed
    assert overview.cache_file(str(tmp_path), 16, 35436, 22173) == tmp_path / "16" / "35436" / "22173.mixed"


def test_cached_tiles_are_the_overview_zooms_over_the_sheet(tmp_path):
    paths = overview.cached_tiles(str(tmp_path), ["602_5554"], 15)
    zooms = {int(p.relative_to(tmp_path).parts[0]) for p in paths}
    assert zooms == set(range(15))
    assert tmp_path / "0" / "0" / "0.mixed" in paths
    # zoom 1: Czechia is in the north-eastern quarter, row 0 counted from the north
    assert {p for p in paths if p.parts[-3] == "1"} == {tmp_path / "1" / "1" / "0.mixed"}


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
    return Store(str(tmp_path / "data"), str(tmp_path / "state"), f"file://{src}", fetch_jobs=2,
                 budget_bytes=10 ** 12, min_free_bytes=0)


def test_ensure_fetches_what_is_missing_then_indexes_it(store):
    store.ensure(["602_5554", "604_5554"])
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
        store.ensure(["606_5554"])
    assert not store.jp2("606_5554").exists() and not list((store.data / ".tmp").iterdir())
    with pytest.raises(SheetMissing):
        store.ensure(["608_5554"])                         # not on the server at all


def test_the_sheets_that_arrived_are_indexed_when_another_fails(store):
    with pytest.raises(SheetMissing, match="608_5554"):
        store.ensure(["602_5554", "608_5554"])
    assert store.jp2("602_5554").exists() and str(store.jp2("602_5554")) in store.index.read_text()


def test_two_requests_fetch_a_sheet_once(store):
    fetched = []
    original = store._fetch
    store._fetch = lambda sid: (fetched.append(sid) or True) if original(sid) else False
    threads = [threading.Thread(target=store.ensure, args=(["602_5554"],)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fetched == ["602_5554"]


def test_at_the_budget_nothing_is_fetched_and_nothing_held_goes(store):
    store.ensure(["602_5554"])
    store.budget = store.held_bytes()                      # exactly at it
    assert store.full()
    with pytest.raises(StoreFull, match="602_5556 not fetched"):
        store.ensure(["602_5556"])
    assert not store.jp2("602_5556").exists()
    store.ensure(["602_5554"])                             # what is held still serves
    serve.maintain_once(SimpleNamespace(ov_min_sheets=99, ov_quiet_s=0, ov_max_age_s=1e9), store, KNOWN)
    assert store.jp2("602_5554").exists()                  # a sheet is never deleted


def test_a_disk_short_of_the_reserve_stops_fetching_too(store):
    store.min_free = 10 ** 18
    with pytest.raises(StoreFull):
        store.ensure(["602_5554"])


def settle(store):
    """Wait for the store's background downloads (begin), as a client's retry would."""
    if store._pool is not None:
        store._pool.shutdown(wait=True)
        store._pool = None


def test_begin_downloads_in_the_background_once_and_then_the_tile_is_ready(store):
    gate = threading.Event()
    original = store.ensure
    store.ensure = lambda ids: (gate.wait(5), original(ids))[1]
    started, after = store.begin(["602_5554", "604_5554"])
    assert started == 2 and 5 <= after <= 120               # answered at once, not after the download
    assert not store.jp2("602_5554").exists()
    assert store.begin(["602_5554"]) == (0, after)          # in flight already: none started again
    gate.set()
    settle(store)
    assert store.begin(["602_5554", "604_5554"]) is None    # held: drawn now
    with store._db() as con:
        assert con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 2


def test_a_sheet_downloading_in_another_worker_is_not_started_again(store):
    with store.lock("602_5554"):                            # another worker holds it: downloading
        assert store.begin(["602_5554"])[0] == 0
    assert store.begin(["602_5554"])[0] == 1
    settle(store)


def test_a_failed_download_is_a_502_for_every_worker_for_a_while_then_tried_again(store, monkeypatch):
    import store as store_mod
    assert store.begin(["608_5554"])[0] == 1                # not on the server at all
    settle(store)
    other = Store(str(store.data), str(store.state), store.url, 1, store.budget, store.min_free)
    with pytest.raises(SheetMissing, match="608_5554"):     # another worker reads the same record
        other.begin(["608_5554"])
    monkeypatch.setattr(store_mod, "FAIL_HOLD_S", 0)
    assert store.begin(["608_5554"])[0] == 1                # the hold is over: tried again
    settle(store)


def test_begin_refuses_once_fetching_has_stopped(store):
    store.min_free = 10 ** 18
    with pytest.raises(StoreFull):
        store.begin(["602_5554"])


def test_retry_after_is_the_recent_downloads_times_the_queue(store):
    assert store.retry_after() == 20                        # nothing measured yet
    with store._db() as con:
        con.executemany("INSERT INTO downloads (id, seconds, at) VALUES (?, ?, ?)",
                        [("a", 30.0, 1), ("b", 40.0, 2), ("c", 50.0, 3)])
    assert store.retry_after() == 40
    store._queued.update({"x", "y", "z"})                   # 3 queued over 2 jobs: two rounds
    assert store.retry_after() == 80
    store._queued.update({str(i) for i in range(20)})
    assert store.retry_after() == 120                       # capped


def test_reconcile_follows_the_disk(store):
    store.ensure(["602_5554", "604_5554"])
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
