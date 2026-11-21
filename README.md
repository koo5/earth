# earth

**earth.hillview.cz**: hillview's public geodata stack. It serves terrain
for the whole earth (Czechia from ČÚZK's lidar, finest), the Czech orthophoto, OpenStreetMap vector
tiles and Prague's 3D buildings, for the hillview frontend and hillview's workbench.
Everything is unauthenticated and rate-limited by IP, and answers only under a short fixed path prefix that
the clients carry (`https://earth.hillview.cz/<prefix>/...`, `deploy/README.md`); anything else is dropped.


## What it serves

| request | what |
|---|---|
| `GET /channels`, `GET /channels/{name}` | which release of each layer a channel serves (`prod`, `beta`) |
| `GET /releases/{id}` | what a release is made from (the mosaics' stamp, the archive, the areas, the generator) |
| `GET /t/{release}/terrain/{surface\|bare}/{z}/{x}/{y}.{q16\|f32}` | a terrain tile: 65 × 65 ellipsoidal heights (the mosaic's + the EGM2008 geoid, 44.45 m at Brandýs nad Labem; said by `X-Tile-Heights: ellipsoidal`) and a source byte per sample (`earth/tiles.py`) |
| `POST /t/{release}/terrain/ground` | the ground at points or over a box, with a status per point (`ok`, `not_fetched`, `outside`, …); orthometric, as the models give it (said by `"vertical": "orthometric"`). `want` is `bare` (DTM10), `surface` (DSM10, else GLO-30) or `bare_finest` (DTM2 and its fill, else what `bare` answers), each point's `src` naming its model (`earth/ground.py`) |
| `GET /t/{release}/ortho/{z}/{x}/{y}.jpg` | ČÚZK Ortofoto ČR (CC BY 4.0), 12.5 cm, from the ortho service as it came, never kept here; Czechia only. A tile whose sheets are still downloading is a 503 with `Retry-After` at once: come back then (`earth/ortho.py`) |
| `GET /t/{release}/osm/{z}/{x}/{y}.mvt` | OpenStreetMap (ODbL) as vector tiles from the release's PMTiles archive: buildings, walls, roads, water with raw tags and `osm_id`, zoom 13-15; 204 for empty ground (`earth/osm.py`) |
| `GET /t/{release}/models/manifest.json`, `.../models/assets/{sha256}.glb` | IPR Praha's 3D buildings (2020, CC BY 4.0) as GLB meshes with ellipsoidal heights, prepared ahead by area and served only as the release's manifest names them; Prague only (`earth/citymodels.py`) |
| `GET /t/{release}/names?lat&lon&radius_m[&chunk&chunks]` | OpenStreetMap's (ODbL) named peaks, observation towers, communication masts, water towers and places (city to quarter) round a point, each at an ellipsoidal height, as the workbench's `/terrain/labels` answers: near-first chunks (`earth/names.py`) |
| `GET /attribution` | what each source requires shown; each tile names its own in `X-Attribution` |
| `GET /<dem secret>/...` | the terrain mosaics themselves, read-only, for GDAL's `/vsicurl/`: served by Caddy under a secret path of their own (`deploy/Caddyfile`), not by earth |

A client reads its channel once and then asks only for release URLs. A frozen release never
changes, so its tiles are `Cache-Control: immutable`. Terrain tiles are made the first time they
are asked for and kept: only complete ones, in SQLite packs, one per zoom-10 block (`earth/store.py`).

## Where each layer comes from

**Terrain** (`builder/`): `build_all.sh` downloads and builds everything the terrain reads into the
DEM directory, incrementally (a rerun resumes; a sheet is written whole or not at all):
- ČÚZK DMR 5G (bare earth) and DMP 1G (surface), every sheet of Czechia (about 16,300 each):
  LAZ → 2 m rasters → WGS84 mosaics at 2 m and 10 m (`auto_cuzk.sh`, `download_cuzk.py`,
  `build_mosaic.py`);
- Copernicus GLO-30 over 5-25 °E, 44-56 °N: the ring beyond the border (`download_glo30.py`);
- ETOPO 2022 at 60 arc-seconds: the whole earth (the tiles serve the sea at 0 m, source byte `sea`);
- the EGM2008 geoid grid, for ellipsoidal heights.

**Orthophoto** (`ortho/`, its own README): a MapServer + MapProxy service, internal to the stack,
that earth's ortho layer asks (`EARTH_ORTHO_URL`). It holds none of ČÚZK's 20,308 JPEG2000 sheets
(2 × 2 km, ~1.5 TB in all) until a view needs them. From zoom 15 a tile is drawn from the sheets
under it, fetched from ČÚZK's open-data endpoint on that first request (seconds); below zoom 15,
from a 4 m overview of Czechia that fetched sheets are warped into in batches. A batch waits for a
quiet spell or an hour, never one per sheet. A sheet is kept for good; at the disk budget
(`SHEET_BUDGET_GB`) fetching stops. earth has no other orthophoto upstream.

**OSM**: `osm/build.sh` runs planetiler with `osm/hillview-map.yml` over Geofabrik's Czech extract
into one `.pmtiles`.

**Names**: `osm/names.sh` pulls the named points out of the same extract (Debian's osmium-tool) and
`earth names build` keeps the workbench's names query and tags (copied from it), filling a missing `ele`
from the surface model and making every height ellipsoidal; one JSON file per release.

**City models**: `earth models prepare` reads IPR Praha's I3S scene for one circular area (at most
2 km) and writes content-addressed GLBs; EGM96 heights are converted with a grid kept in the store.

## Releases

Each layer's release is built and registered on the server with the `earth` CLI, and a channel then
points at it (the commands: `deploy/README.md`). A frozen release refuses to make tiles once its
mosaics change: new sheets mean a new release. A machine whose ground is still being built registers a
`--live` terrain release instead. It follows the mosaics, keeps its tiles per mosaic stamp, and is never
served as immutable.

Before a terrain release is registered, each ČÚZK mosaic (bare and surface, 10 m and 2 m) is read at the
summits in `earth/reference_heights.json` (open summits, their OSM `ele`, one per ~25 km), and the release is
refused when one reads more than 5 m off, or when a mosaic has none on it (`earth/reference.py`). The first
full build here came out 44 m high: GDAL 3.13 turned the warp's sea-level heights ellipsoidal. The release
records what was checked, or `--unchecked` and why.

## Limits

Each IP has two token buckets: reads of kept tiles, and makes of new ones. On top of that, a
cap limits how many makes run at once (`earth/limits.py`). An orthophoto tile is a read, and what
the ortho service says it cost (sheets fetched, a tile drawn) is charged to the make bucket after it;
an address in debt waits. The settings are
`EARTH_LIMIT_{READ,MAKE}_{RATE,BURST}`, `EARTH_LIMIT_MAKE_CONCURRENT` and `EARTH_NO_LIMITS=1`.
`X-Forwarded-For` is believed only from `EARTH_TRUSTED_PROXIES` (the proxy in front).

## The repository

| path | what |
|---|---|
| `earth/` | the service (FastAPI): tiles, ground, releases and channels, limits, attribution; every path an environment variable (`earth/config.py`; the defaults are the `/dem` layout the builder writes) |
| `builder/` | the terrain builder, run as the compose service `builder` (`deploy/builder.Dockerfile`) |
| `ortho/` | the orthophoto service, the compose service `ortho` |
| `osm/` | the OSM build and its planetiler schema |
| `deploy/` | compose, Caddy, Garage, the server's layout and `.env`, publishing releases, the unhealthy-container timer (`deploy/README.md`) |
| `tests/` | earth's tests and the builder's (`tests/builder/`); the ortho service's are in `ortho/tests/` |

## License

The code is under the GNU Affero General Public License, version 3 (`LICENSE`). Two files in the
repository are OpenStreetMap data, under the ODbL (© OpenStreetMap contributors), not the AGPL:
`earth/reference_heights.json` (the summits a terrain release is checked at) and
`ortho/app/places.json` (the towns the ortho service fetches ahead). What earth serves keeps its
sources' licences, and `GET /attribution` says what each requires shown (`earth/attribution.py`).

## Develop

```sh
uv sync && uv run pytest                                   # earth + builder
CUZK_FIXTURE_DIR=<dir with a sheet's .jp2> ortho/tests/run.sh   # builds the ortho image and runs it
uv run uvicorn earth.app:app --reload
```

The one deployment is earth.hillview.cz (`deploy/README.md`). `CONTAINER_CLI=podman` runs the ortho
tests under Podman. Dependencies keep the
14-day cooloff (`exclude-newer` in `pyproject.toml`). `earth/ground.py`, `earth/geodesy.py` and the
tile code came from the workbench's API on 2026-10-13. Until the workbench switches to this service,
a fix to either copy belongs in the other too. `tests/geodesy-cases.json` is `hillview_common`'s
`world/geodesy-cases.json`, and a test checks that the two match.
