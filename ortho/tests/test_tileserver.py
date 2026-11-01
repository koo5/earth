"""Black-box behaviour of the containerised tile server. Independent of whether
the entrypoint is bash or Python — all assertions are on endpoints / container."""
import concurrent.futures
import io
import subprocess

from PIL import Image

from helpers import CLI, CONTAINER, FIXTURE_TILE, TILE_CENTER, deg2tile, http


def _alpha_range(body):
    im = Image.open(io.BytesIO(body)).convert("RGBA")
    return im.getchannel("A").getextrema()


def test_capabilities_reachable(server):
    status, _, body = http("/wmts/1.0.0/WMTSCapabilities.xml")
    assert status == 200
    assert b"Capabilities" in body


def test_viewer_served_at_map(server):
    status, ctype, body = http("/map")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"leaflet" in body.lower()


def test_there_is_no_demo(server):
    assert http("/demo/")[0] == 404


def test_there_is_no_wms(server):
    assert http("/service?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0")[0] != 200


def test_a_detail_tile_fetches_its_sheet_once(fetched):
    r = subprocess.run([CLI, "exec", CONTAINER, "sh", "-c", "ls /data; cat /state/sheets.csv"],
                       capture_output=True, text=True)
    assert f"{FIXTURE_TILE.stem}.jp2" in r.stdout and f"/data/{FIXTURE_TILE.stem}.jp2," in r.stdout
    status, _, body = http("/status")
    assert status == 200 and b'"sheets_held": 1' in body


def test_a_sheet_that_does_not_exist_is_a_502_not_a_tile(server):
    # a sheet in ČÚZK's list but not in this test's source: never drawn without it
    x, y = deg2tile(49.2, 16.6, 16)            # Brno, far from the fixture
    assert http(f"/wmts/oi/webmercator/16/{x}/{y}.png", timeout=60)[0] == 502


def test_only_the_rest_tile_route_renders(server):
    x, y = deg2tile(*TILE_CENTER, 16)
    assert http(f"/tms/1.0.0/oi/webmercator/16/{x}/{2 ** 16 - 1 - y}.png")[0] == 404
    kvp = (f"/service?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=oi&STYLE=default&FORMAT=image/png"
           f"&TILEMATRIXSET=webmercator&TILEMATRIX=16&TILEROW={y}&TILECOL={x}")
    assert http(kvp)[0] != 200


def test_interior_highzoom_is_opaque_jpeg(fetched):
    # In-data, fully-opaque tile -> mixed format emits JPEG.
    x, y = fetched
    status, ctype, body = http(f"/wmts/oi/webmercator/16/{x}/{y}.png")
    assert status == 200
    assert ctype == "image/jpeg", f"opaque interior tile should be JPEG (mixed), got {ctype}"
    assert _alpha_range(body) == (255, 255), "interior tile must be fully opaque"


def test_lowzoom_overview_has_the_sheet_and_transparency_round_it(overview_ready):
    # A z9 tile dwarfs a single 2 km sheet: the overview drew the sheet (opaque) and nothing round it.
    x, y = deg2tile(*TILE_CENTER, 9)
    status, ctype, body = http(f"/wmts/oi/webmercator/9/{x}/{y}.png")
    assert status == 200
    assert ctype == "image/png", f"partly-transparent tile must be PNG, got {ctype}"
    assert _alpha_range(body) == (0, 255), "z9 tile: the sheet opaque, the rest transparent"


def test_lock_dirs_live_outside_cache(server):
    # Regression: tile locks must not sit under /cache, or the refresh loop's
    # `rm -rf /cache/*` deletes them mid-render -> 500 "Could not create Lock-file".
    r = subprocess.run(
        [CLI, "exec", CONTAINER, "sh", "-c",
         "find /cache -iname '*lock*' 2>/dev/null; echo ---; ls -d /tmp/mapproxy/tile_locks 2>/dev/null"],
        capture_output=True, text=True)
    before, _, after = r.stdout.partition("---")
    assert "/tmp/mapproxy/tile_locks" in after, "tile_lock_dir should live under /tmp"
    assert "lock" not in before.lower(), f"no locks should live under /cache; found: {before!r}"


def test_no_500_under_concurrent_cache_clear(fetched):
    # Regression for the z15 lock-file race: clear the cache while hammering.
    x, y = deg2tile(*TILE_CENTER, 15)
    url = f"/wmts/oi/webmercator/15/{x}/{y}.png"
    codes = []
    for _ in range(3):
        subprocess.run([CLI, "exec", CONTAINER, "sh", "-c", "rm -rf /cache/* 2>/dev/null || true"],
                       capture_output=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(http, url) for _ in range(8)]
            codes += [f.result()[0] for f in futures]
    assert all(c == 200 for c in codes), f"non-200 under concurrency + cache-clear: {codes}"
