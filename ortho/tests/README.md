# Tests

```sh
CUZK_FIXTURE_DIR=<dir with a sheet's .jp2 and .j2w> ./tests/run.sh      # -v, -k ... pass through
```

`CONTAINER_CLI=podman` runs the container suite under Podman. The first run builds the image (apt +
pip, slow); later runs hit the layer cache. The container is `cuzk-oi-test` on port 18080, so it
never collides with a running service.

**`test_units.py`** (no container): the sheet list, the sheets under a box and a tile, the margin,
the crossover check, the overview's hysteresis, which cached tiles an update drops, and the store
(fetching, a failed fetch leaving nothing, one fetch for two requests, eviction and the renders it
waits for, reconcile), against `file://` sheets made in a temp dir. Also that white is imagery, not
nodata.

**`test_tileserver.py`** (one container): the fixture sheet is zipped as ČÚZK ships it into a local
directory that is the container's `SHEETS_URL`, so the container fetches it itself. It asserts:
capabilities and `/map` answer; there is no demo and no WMS; a detail tile fetches its sheet once and
indexes it; a sheet missing from the source is a 502, not a tile; only the REST tile route renders;
an interior tile is an opaque JPEG; the overview takes the sheet with transparency round it; locks
live outside `/cache`, and deleting the cache under load gives no 500.

The fixture is the largest sheet in `CUZK_FIXTURE_DIR`; without the variable the container suite
skips.
