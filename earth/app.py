"""The earth service: hillview's public geodata, unauthenticated, rate-limited by IP.

  GET /health
  GET /channels, /channels/{name}       which release of each layer a channel serves
  GET /releases/{id}                    what a release is made from
  GET /attribution                      what each source requires shown
  GET /t/{release}/terrain/{model}/{z}/{x}/{y}.{q16|f32}
                                        a terrain tile (tiles.py), made on first ask and kept
  POST /t/{release}/terrain/ground      the ground at given points, or over a box (ground.py)
  GET /t/{release}/ortho/{z}/{x}/{y}.jpg
                                        the orthophoto (ortho.py), from the ortho service, never kept
  GET /t/{release}/osm/{z}/{x}/{y}.mvt  the map (osm.py), from the release's PMTiles archive
  GET /t/{release}/names?lat&lon&radius_m[&chunk&chunks]
                                        named peaks, towers and places round a point (names.py)
  GET /t/{release}/models/manifest.json the city models (citymodels.py): the release's manifest
  GET /t/{release}/models/assets/{sha256}.glb
                                        one of its meshes, only those its manifest names

Clients read a channel once, at start-up, and then only release URLs, which never change
(store.py).
"""
from __future__ import annotations

import gzip

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import attribution, citymodels, config, ground as ground_mod, names, ortho, osm, store as store_mod, tiles
from .limits import Limits, client_ip

IMMUTABLE = "public, max-age=31536000, immutable"
LIVE = "public, max-age=300"
TILE_HEADERS = ["X-Tile-N", "X-Tile-Bounds", "X-Tile-Model", "X-Tile-Status", "X-Tile-Sources",
                "X-Tile-Heights", "X-Tile-Format", "X-Tile-Complete", "X-Tile-Waiting", "X-Tile-Kept",
                "X-Attribution",
                # not CORS-safelisted: without it a page on another origin reads null, and retries a 429 or a
                # downloading ortho tile's 503 on its own backoff instead of when it was asked to
                "Retry-After"]


def create_app(store: store_mod.Store | None = None, limits: Limits | None = None) -> FastAPI:
    app = FastAPI(title="earth", docs_url=None, redoc_url=None, openapi_url=None)
    # everything is public and read-only: any origin may read it, and see the tile headers. POST
    # is a ground query, which changes nothing either
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], expose_headers=TILE_HEADERS, max_age=86400)
    state: dict = {"store": store, "limits": limits}

    def st() -> store_mod.Store:
        if state["store"] is None:
            state["store"] = store_mod.Store()
        return state["store"]

    def lim() -> Limits:
        if state["limits"] is None:
            state["limits"] = Limits()
        return state["limits"]

    @app.get("/health")
    async def health():
        # on the loop, not the thread pool: a pool full of slow work is not a dead process (on 2026-11-02 ortho
        # asks waiting for upstream slots held every thread, /health timed out, and the heal restarted earth)
        return {"ok": True}

    @app.get("/channels")
    def channels(request: Request):
        lim().check_read(request)
        return JSONResponse({"channels": st().channels()}, headers={"Cache-Control": "public, max-age=30"})

    @app.get("/channels/{name}")
    def channel(name: str, request: Request):
        lim().check_read(request)
        ch = st().channel(name)
        if ch is None:
            raise HTTPException(404, f"no channel {name}")
        return JSONResponse(ch, headers={"Cache-Control": "public, max-age=30"})

    @app.get("/releases/{rid}")
    def release(rid: str, request: Request):
        lim().check_read(request)
        r = st().release(rid)
        if r is None:
            raise HTTPException(404, f"no release {rid}")
        return JSONResponse({"id": r.id, "layer": r.layer, "mode": r.mode, "created_at": r.created_at,
                             "generator": r.generator, "sources": r.sources, "note": r.note},
                            headers={"Cache-Control": IMMUTABLE if r.immutable else LIVE})

    @app.get("/attribution")
    def attribution_all():
        return JSONResponse({"sources": attribution.SOURCES}, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/t/{rid}/terrain/{model}/{z}/{x}/{y}.{fmt}")
    async def terrain_tile(rid: str, model: str, z: int, x: int, y: int, fmt: str, request: Request):
        lim().check_read(request)
        r = st().release(rid)
        if r is None or r.layer != "terrain":
            raise HTTPException(404, f"no terrain release {rid}")
        if model not in tiles.MODELS or fmt not in ("q16", "f32") or not tiles.valid(z, x, y):
            raise HTTPException(404, f"no tile {model}/{z}/{x}/{y}.{fmt} (zoom 0..{tiles.MAX_Z})")
        if r.immutable:
            if model not in r.sources:
                raise HTTPException(404, f"release {rid} has no {model} model")
            stamp = store_mod.stamp_key(r.sources[model])
        else:
            stamp = store_mod.stamp_key(tiles.source_stamp(model))
        gz_ok = "gzip" in (request.headers.get("accept-encoding") or "")

        def send(gz: bytes, meta: dict, kept: bool, cache: str) -> Response:
            h = meta | {"X-Tile-Kept": "1" if kept else "0", "Cache-Control": cache, "Vary": "Accept-Encoding"}
            if gz_ok:
                return Response(gz, media_type="application/octet-stream", headers=h | {"Content-Encoding": "gzip"})
            return Response(gzip.decompress(gz), media_type="application/octet-stream", headers=h)

        hit = await run_in_threadpool(st().get_tile, rid, model, z, x, y, fmt, stamp)
        if hit is not None:
            return send(hit[0], hit[1], True, IMMUTABLE if r.immutable else LIVE)

        lim().check_make(request)
        if r.immutable and store_mod.stamp_key(tiles.source_stamp(model)) != stamp:
            # the mosaics are not the ones this release was registered on: what it already holds
            # is still served, but nothing new is made from other data under its name
            raise HTTPException(409, f"the {model} mosaics changed since release {rid} was registered; "
                                     f"its kept tiles are served, new ones need a new release")
        async with lim().making:
            try:
                t = await run_in_threadpool(tiles.make, z, x, y, model)
            except tiles.TileError as e:
                raise HTTPException(503, str(e))
        body, sent = t.encode(fmt)
        used = t.used()
        w, s_, e, n = tiles.tile_bounds(z, x, y)
        meta = {"X-Tile-N": str(tiles.N), "X-Tile-Bounds": f"{w},{s_},{e},{n}", "X-Tile-Model": model,
                "X-Tile-Status": ",".join(f"{k}:{v}" for k, v in t.counts.items() if v),
                "X-Tile-Sources": ",".join(f"{k}:{v}" for k, v in used.items()),
                "X-Tile-Heights": "ellipsoidal", "X-Tile-Format": sent,
                "X-Tile-Complete": "1" if t.complete else "0",
                "X-Attribution": ",".join(attribution.for_tile(used))}
        if t.waiting:
            meta["X-Tile-Waiting"] = ",".join(t.waiting)
        gz = gzip.compress(body, 6)
        if t.complete:
            await run_in_threadpool(st().put_tile, rid, model, z, x, y, fmt, stamp, gz, meta)
            return send(gz, meta, False, IMMUTABLE if r.immutable else LIVE)
        # not complete: answered, not kept, and asked again next time
        return send(gz, meta, False, "no-store")

    @app.post("/t/{rid}/terrain/ground")
    async def terrain_ground(rid: str, q: GroundQuery, request: Request):
        """The ground at each point, with a status per point (ground.sample_ground): heights as
        the models give them (orthometric), not the tiles' ellipsoidal ones. Nothing is fetched
        here: a not_fetched point waits for whoever builds the mosaics.

        -> {release, want, stamp, vertical, heights, src, status, counts, sources, points (box only)}
        `heights[i]` is null unless `status[i]` is "ok". `stamp` names the mosaics the heights were read from
        (store.stamp_key of tiles.source_stamp(want)): for a frozen release its registered one, for a live one
        what was on disk then, so a caller can record which terrain a height came from (RESEARCH3's ea10). A
        live release's mosaics rebuilt during the query make it a 503, Retry-After 1: never heights under a
        stamp they were not all read from."""
        lim().check_read(request)
        r = _terrain_release(rid)
        if (q.points is None) == (q.bbox is None):
            raise HTTPException(422, "give exactly one of points or bbox")
        cap = config.ground_max_points()
        if q.bbox is not None:
            if len(q.bbox) != 4:
                raise HTTPException(422, "bbox must be [w, s, e, n]")
            try:
                pts = ground_mod.grid(q.bbox, q.step_m, cap)
            except ValueError as e:
                raise HTTPException(422, str(e))
        else:
            assert q.points is not None
            if any(len(p) != 2 for p in q.points):
                raise HTTPException(422, "each point is [lon, lat]")
            if len(q.points) > cap:
                raise HTTPException(422, f"{len(q.points)} points, past {cap}")
            pts = q.points
        lim().check_make(request, max(1.0, len(pts) / tiles.N ** 2))
        if r.immutable:
            if q.want not in r.sources:
                raise HTTPException(404, f"release {rid} has no {q.want} model")
            if store_mod.stamp_key(tiles.source_stamp(q.want)) != store_mod.stamp_key(r.sources[q.want]):
                raise HTTPException(409, f"the {q.want} mosaics changed since release {rid} was "
                                         f"registered; ask a newer release")
        async with lim().making:
            stamp = store_mod.stamp_key(tiles.source_stamp(q.want))
            try:
                g = await run_in_threadpool(ground_mod.sample_ground, [p[0] for p in pts],
                                            [p[1] for p in pts], q.want, single_source=q.single_source)
            except ground_mod.GroundConfigError as e:
                raise HTTPException(503, str(e))
            if store_mod.stamp_key(tiles.source_stamp(q.want)) != stamp:
                raise HTTPException(503, f"the {q.want} mosaics changed during this query; ask again",
                                    headers={"Retry-After": "1"})
        out = {"release": rid, "want": q.want, "stamp": stamp, "vertical": "orthometric",
               "heights": [round(float(h), 3) if st == "ok" else None for h, st in zip(g.height, g.status)],
               "src": g.src, "status": g.status, "counts": g.counts(), "sources": g.sources()}
        if q.bbox is not None:
            out["points"] = pts
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get("/t/{rid}/ortho/{z}/{x}/{y}.jpg")
    async def ortho_tile(rid: str, z: int, x: int, y: int, request: Request):
        """ČÚZK's orthophoto tile from the ortho service, as it came (JPEG; PNG where it holds no sheet,
        said by Content-Type). Nothing is kept here: the ortho service caches what it draws and drops
        what a new sheet changes, so a copy here would go stale. A read, plus what the ortho service
        says the tile cost (sheets fetched, a tile drawn) charged to the make bucket. The address's slot,
        then one of everyone's, are waited for on the loop (no thread held); the debt check runs once
        both are held and the charge before they go, so a request checked after another finished sees
        that one's charge, and an address has at most its slots in flight past its check."""
        lim().check_read(request)
        r = st().release(rid)
        if r is None or r.layer != "ortho":
            raise HTTPException(404, f"no ortho release {rid}")
        if not (0 <= z <= ortho.MAX_Z and 0 <= x < 1 << z and 0 <= y < 1 << z):
            raise HTTPException(404, f"no ortho tile {z}/{x}/{y} (zoom 0..{ortho.MAX_Z})")
        if not ortho.covered(z, x, y):
            raise HTTPException(404, "no orthophoto outside Czechia")
        async with lim().ortho_address.hold(client_ip(request)), lim().ortho_upstream:
            lim().check_owed(request)
            try:
                got = await run_in_threadpool(ortho.fetch, z, x, y)
            except ortho.OrthoError as e:
                lim().charge_make(request, e.cost)         # a 503 for a tile still downloading: what it started
                raise HTTPException(e.status, e.detail,
                                    headers={"Retry-After": e.retry_after} if e.retry_after else None)
            if got is None:
                raise HTTPException(404, f"no orthophoto at {z}/{x}/{y}")
            body, mt, cost = got
            lim().charge_make(request, cost)
        return Response(body, media_type=mt,
                        headers={"Cache-Control": LIVE, "X-Attribution": "cuzk-ortho"})

    @app.get("/t/{rid}/osm/{z}/{x}/{y}.mvt")
    async def osm_tile(rid: str, z: int, x: int, y: int, request: Request):
        lim().check_read(request)
        r = st().release(rid)
        if r is None or r.layer != "osm":
            raise HTTPException(404, f"no osm release {rid}")
        a = await run_in_threadpool(osm.archive, r.sources["pmtiles"])
        lo, hi = a.zooms
        if not (lo <= z <= hi and 0 <= x < 1 << z and 0 <= y < 1 << z):
            raise HTTPException(404, f"no map tile {z}/{x}/{y} (zoom {lo}..{hi}; finer is overzoomed)")
        h = {"Cache-Control": IMMUTABLE if r.immutable else LIVE, "X-Attribution": "osm", "Vary": "Accept-Encoding"}
        body = await run_in_threadpool(a.tile, z, x, y)
        if body is None:
            return Response(status_code=204, headers=h)
        if a.gzipped:
            if "gzip" in (request.headers.get("accept-encoding") or ""):
                return Response(body, media_type=osm.MEDIA_TYPE, headers=h | {"Content-Encoding": "gzip"})
            body = gzip.decompress(body)
        return Response(body, media_type=osm.MEDIA_TYPE, headers=h)

    @app.get("/t/{rid}/names")
    async def names_disc(rid: str, lat: float, lon: float, request: Request, radius_m: float = 120_000.0,
                         chunk: int | None = None, chunks: int = 1):
        """The names within radius_m of a point, as the workbench's /terrain/labels answers (names.py): near-first
        chunks with `chunk`/`chunks`. A release never changes, so neither does an answer."""
        lim().check_read(request)
        r = st().release(rid)
        if r is None or r.layer != "names":
            raise HTTPException(404, f"no names release {rid}")
        try:
            n = await run_in_threadpool(names.release, r.sources["names"])
        except (names.NamesError, OSError) as e:          # the release's file: the server's fault
            raise HTTPException(500, f"names release {rid}: {e}")
        try:
            out = n.disc(lat, lon, radius_m, chunk, chunks)
        except names.NamesError as e:                     # the point asked: the client's
            raise HTTPException(422, str(e))
        return JSONResponse(out, headers={"Cache-Control": IMMUTABLE if r.immutable else LIVE, "X-Attribution": "osm"})

    models: dict[str, citymodels.Release] = {}

    def _models(rid: str) -> citymodels.Release:
        r = st().release(rid)
        if r is None or r.layer != "models":
            raise HTTPException(404, f"no models release {rid}")
        m = models.get(rid)
        if m is None:
            m = models[rid] = citymodels.Release(r.sources["manifest"])
        return m

    @app.get("/t/{rid}/models/manifest.json")
    async def models_manifest(rid: str, request: Request):
        lim().check_read(request)
        m = await run_in_threadpool(_models, rid)
        return Response(m.body, media_type="application/json",
                        headers={"Cache-Control": IMMUTABLE, "X-Attribution": "ipr"})

    @app.get("/t/{rid}/models/assets/{sha}.glb")
    async def models_asset(rid: str, sha: str, request: Request):
        lim().check_read(request)
        m = await run_in_threadpool(_models, rid)
        path = m.asset_path(sha)
        if path is None:
            raise HTTPException(404, f"release {rid} has no mesh {sha}")
        body = await run_in_threadpool(path.read_bytes)
        return Response(body, media_type="model/gltf-binary",
                        headers={"Cache-Control": IMMUTABLE, "ETag": f'"{sha}"', "X-Attribution": "ipr"})

    def _terrain_release(rid: str) -> store_mod.Release:
        r = st().release(rid)
        if r is None or r.layer != "terrain":
            raise HTTPException(404, f"no terrain release {rid}")
        return r

    return app


class GroundQuery(BaseModel):
    points: list[list[float]] | None = None       # [[lon, lat], ...]
    bbox: list[float] | None = None               # [w, s, e, n], sampled every step_m
    step_m: float = 10.0
    want: Literal["bare", "surface"] = "bare"
    single_source: bool = False                   # surface: refuse a batch spanning ČÚZK and GLO-30


app = create_app()
