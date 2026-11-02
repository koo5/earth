# ortho: ČÚZK's orthophoto, sheet by sheet as it is asked for

Serves the Czech national orthophoto (ČÚZK Ortofoto ČR, INSPIRE theme **OI**, 12.5 cm, CC BY 4.0)
as Web Mercator tiles, rendered from ČÚZK's original JPEG2000 sheets. It holds none of them at
first: a sheet is fetched from ČÚZK's open-data endpoint the first time a tile needs it, and kept for
good; when the disk budget is reached, fetching stops (more storage is the answer, not eviction). It is earth's orthophoto upstream
(`EARTH_ORTHO_URL`, the compose service `ortho`), reachable only from earth's network. It began as
a tile server that held all ~1.5 TB of sheets.

```
ČÚZK openzu  <id>.zip ──(the first detail tile over it)──► /data/<id>.jp2 (+ .j2w, .gml)
                                     │                          │  batches (overview.py)
                       /state/sheets.csv (tile index)           ▼
                                     │               /state/overview.tif (4 m, sparse, alpha)
                                     ▼                          ▼
                    MapServer 8:  oi (detail, zoom ≥ 15)    oi_ov (overview, zoom < 15)
                                     │
                    MapProxy (EPSG:25833 → 3857, file cache /cache/oi, mixed JPEG/PNG)
                                     │
                         /wmts/oi/webmercator/{z}/{x}/{y}.png
```

## The sheets

- **Which exist:** `app/sheets.txt`, 20,308 sheets with ČÚZK's last update each, made by
  `make_sheets.py` from ČÚZK's INSPIRE Atom feed (`python3 make_sheets.py > app/sheets.txt`).
- **A sheet** is 2 × 2 km at 12.5 cm (16000 × 16000 px), named by its lower-left corner in ETRS89/TM33
  kilometres: `602_5554` covers E 602000-604000, N 5554000-5556000. The data is EPSG:3045, treated
  as EPSG:25833 (the same projection, east-north axis order). So the sheets under a tile are
  arithmetic on the tile's box (`app/sheets.py`), with 2 m to spare for the resampling.
- **Fetching** (`app/store.py`): `<id>.zip` (~75 MB) from `SHEETS_URL`, several at once, a lock
  per sheet so two requests fetch it once. The sidecars are published first and the `.jp2` last by
  an atomic rename, so a reader never sees half a sheet. A sheet that is not there (404) is not
  retried; another failure is, up to four tries.
- **Rendering:** a detail tile is drawn only once every sheet under it is on disk. A first view of
  an area waits for its sheets (seconds); a sheet that cannot be fetched is a 502 and a full store a
  503, never a tile drawn with a hole that the cache would keep.
- **The index** MapServer reads is `/state/sheets.csv` (each held sheet's path and its 2 km square),
  rewritten atomically on every change. `/state/sheets.sqlite` records each sheet's size, arrival
  and whether the overview has it yet. At start the record follows the disk.
- **Full:** no sheet is ever deleted, so none is downloaded twice. Once the sheets reach
  `SHEET_BUDGET_GB`, or the disk's free space falls below `MIN_FREE_GB` (the disk is shared), nothing
  more is fetched: a tile that needs a sheet not held is a 503, `/status` says `"full": true`, and
  what is held still serves. A few fetches in flight may pass the line together.
- **Updates:** a held sheet stays as it was fetched; ČÚZK's newer imagery of it is not fetched again.

## The overview (zooms below 15)

A zoomed-out tile would otherwise open thousands of sheets. Below the crossover (`CROSSOVER`, a
scale denominator) MapServer draws from one 4 m mosaic of Czechia instead. It is created once at
the first start, empty and sparse (122000 × 70500 px, with its pyramid): until sheets arrive the
low zooms are transparent. Fetched sheets are warped into it in place, and only their extents of
the pyramid are refreshed (`gdaladdo --partial-refresh-from-source-extent`). Then the cached tiles
of the overview zooms over them are deleted.

Never per sheet: an update runs when at least `OV_MIN_SHEETS` sheets wait and none arrived for
`OV_QUIET_S` (a burst of browsing settles first), or when the oldest has waited `OV_MAX_AGE_S`
(a trickle still gets in). The background maintainer checks every `MAINTAIN_S`.

The crossover is checked at start: zoom 15 must be the first zoom drawn from the sheets at every
Czech latitude, under both of MapServer's pixel conventions, or the service refuses to start. Only
then does a tile's zoom alone say whether it needs sheets.

## White and transparency

Every sheet is opaque, white included. ČÚZK fills the part of a border sheet outside Czechia with exact white;
keying white as nodata would also blank white roofs, cars and the snow on the border ridges, and
make most tiles PNG. So outside the border, within a sheet, shows white. Only where no sheet
exists, or the overview has none yet (its alpha band), is transparent. The cache is
`format: mixed`: JPEG for an opaque tile (~36 KB at zoom 16), PNG where any pixel is transparent.
The `.png` in the URL is only a path; the `Content-Type` says which.

## The tile cache

MapProxy keeps tiles up to zoom 17 in `/cache/oi` (~5 MB per sheet at zooms 15-17). Zooms 18-20 are
drawn from the sheets each time and never stored: stored, they would come to ~350 MB per 75 MB sheet.
Every tile answers `X-Ortho-Cost`: the sheets fetched for it plus 1 if it was drawn rather than taken
from the cache. This service sees only earth, which charges that cost to the client who asked
(`../earth/limits.py`).

## Endpoints (port 8080)

| what | URL |
|---|---|
| a tile | `/wmts/oi/webmercator/{z}/{x}/{y}.png` |
| WMTS capabilities | `/wmts/1.0.0/WMTSCapabilities.xml` |
| state: sheets held, bytes, budget, whether full, overview queue, detail zoom | `/status` (JSON) |
| a Leaflet viewer | `/map` |

That tile route is the only one that renders. MapProxy's WMS, demo, TMS and WMTS KVP are not
configured, and only gunicorn on `app/wsgi.py` serves: any of them would draw a detail tile without
fetching its sheets and cache it for good. Any other `/wmts/` path is a 404.

## Environment

| variable | default | what |
|---|---|---|
| `SHEETS_URL` | `https://openzu.cuzk.gov.cz/opendata/OI` | where `<id>.zip` is fetched from (a `file://` directory works: the tests) |
| `FETCH_JOBS` | `4` | sheets fetched at once by one request |
| `SHEET_BUDGET_GB` | `450` | disk for held sheets (~6,000, ~30% of Czechia); past it nothing is fetched |
| `MIN_FREE_GB` | `50` | and nothing either while the disk has less free than this |
| `OV_MIN_SHEETS`, `OV_QUIET_S`, `OV_MAX_AGE_S` | `8`, `300`, `3600` | the overview's hysteresis |
| `OV_THREADS` | `2` | gdalwarp threads for an overview update |
| `OV_RES` | `4` | the overview's resolution, metres |
| `CROSSOVER` | `16000` | the scale denominator between the overview and the sheets (zoom 14 / 15) |
| `MAINTAIN_S` | `30` | the maintainer's pass interval |
| `DATA_DIR`, `STATE_DIR`, `CACHE_DIR` | `/data`, `/state`, `/cache` | sheets; index, record and overview; MapProxy's tiles |
| `LOCK_DIR`, `TILE_LOCK_DIR` | `/tmp/mapproxy/locks`, `/tmp/mapproxy/tile_locks` | MapProxy's locks, outside the cache (below) |
| `WORKERS`, `THREADS`, `PORT` | `4`, `8`, `8080` | gunicorn (gthread): a tile waiting for its sheet holds a thread, not a worker |
| `SRS` | `EPSG:25833` | the sheets' CRS |
| `BUILD_LOG_INTERVAL` | `15` | seconds between progress lines of a long GDAL step |

MapProxy's locks live in `/tmp/mapproxy`, outside the cache, so deleting cached tiles can never
delete a lock mid-render.

## Running

The compose service `ortho` in `../deploy/compose.yml`, with `/data`, `/state` and `/cache` under
`ortho/` beside the checkout (`../deploy/README.md`).

## The code

| path | what |
|---|---|
| `app/entrypoint.py` | check the crossover, reconcile the held sheets, write the configs, start the maintainer, serve |
| `app/sheets.py` | sheet ids, boxes, the sheets under a tile, the detail zoom |
| `app/store.py` | fetching, the index, the record, the budget |
| `app/overview.py` | the hysteresis, creating and updating the overview, the cached tiles to drop |
| `app/serve.py` | the maintainer thread and the gunicorn child |
| `app/wsgi.py` | `/map`, `/status`, and the tile route behind the sheet fetch, with its cost |
| `app/config.py` | the environment, and the one source of the mapfile and `mapproxy.yaml` |
| `app/build.py` | GDAL steps with progress lines, and the build lock |
| `app/templates/oi.map.tmpl` | the MapServer mapfile |
| `app/sheets.txt`, `make_sheets.py` | the sheets that exist, and how the list is made |
| `tests/` | unit tests and a container suite (`tests/README.md`) |
| `Dockerfile`, `requirements.{in,txt}` | Debian trixie (GDAL 3.10 with JP2OpenJPEG) with MapServer 8.6 from trixie-backports, MapProxy + gunicorn pinned with hashes |
