"""Publishing by hand: register releases, point channels at them.

  earth release register terrain terrain-2026-10-14a [--live] [--note TEXT]
  earth release list
  earth channel set prod terrain=terrain-2026-10-14a [--note TEXT]
  earth channel list
  earth models prepare --lat 50.0909 --lon 14.4005 --radius 600 [--refresh]
  earth release register models models-2026-10-16a --areas <sha256>[,<sha256>...]

A terrain release records the stamp (size, mtime) of every mosaic it reads, per model, at
registration: copy the mosaics into place first, then register. See store.py.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import citymodels, config, names, ortho, osm, reference, store as store_mod, tiles

GENERATOR = "earth-terrain/1 q16+f32 n65"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="earth")
    sub = p.add_subparsers(dest="what", required=True)
    rel = sub.add_parser("release").add_subparsers(dest="cmd", required=True)
    reg = rel.add_parser("register")
    reg.add_argument("layer", choices=["terrain", "ortho", "osm", "models", "names"])
    reg.add_argument("id")
    reg.add_argument("--live", action="store_true", help="follow the mosaics as they change (a workbench); never immutable")
    reg.add_argument("--note", default="")
    reg.add_argument("--year", help="ortho: the imagery's year(s), as its attribution shows them (ČÚZK, <year>)")
    reg.add_argument("--pmtiles", help="osm: the archive's file name in the store's osm/ directory (osm/build.sh)")
    reg.add_argument("--areas", help="models: the prepared areas' sha256s, comma-separated (earth models prepare)")
    reg.add_argument("--names", help="names: the file's name in the store's names/ directory (earth names build)")
    reg.add_argument("--unchecked", metavar="WHY", help="terrain: register without the reference summits "
                     "(reference.py), saying why; the release records it")
    rel.add_parser("list")
    ch = sub.add_parser("channel").add_subparsers(dest="cmd", required=True)
    cs = ch.add_parser("set")
    cs.add_argument("name")
    cs.add_argument("releases", nargs="+", help="layer=release-id")
    cs.add_argument("--note", default="")
    ch.add_parser("list")
    mo = sub.add_parser("models").add_subparsers(dest="cmd", required=True)
    mp = mo.add_parser("prepare", help="fetch and convert IPR Praha's buildings round a point (citymodels.py)")
    mp.add_argument("--lat", type=float, required=True)
    mp.add_argument("--lon", type=float, required=True)
    mp.add_argument("--radius", type=float, required=True, help=f"metres, at most {citymodels.MAX_RADIUS_M}")
    mp.add_argument("--refresh", action="store_true", help="refetch the source indexes; earlier bytes are kept")
    nm = sub.add_parser("names").add_subparsers(dest="cmd", required=True)
    nb = nm.add_parser("build", help="osm/names.sh's export -> a names file, heights from this machine's mosaics")
    nb.add_argument("geojsonseq")
    nb.add_argument("--out", required=True, help="the file to write; for a release, in the store's names/ directory")
    a = p.parse_args(argv)
    if a.what == "names":
        try:
            print(json.dumps(names.build(a.geojsonseq, a.out)))
        except (names.NamesError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if a.what == "models":
        try:
            sha, area = citymodels.prepare(config.egm96_grid(), a.lat, a.lon, a.radius, a.refresh)
        except citymodels.CityModelError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps({"area": sha, "revision": area["revision"], "assets": len(area["assets"]),
                          "triangles": sum(x["triangles"] for x in area["assets"])}))
        return 0
    s = store_mod.Store()
    try:
        if a.what == "release" and a.cmd == "register" and a.unchecked is not None and a.layer != "terrain":
            raise store_mod.StoreError("--unchecked is for a terrain release (its reference summits)")
        if a.what == "release" and a.cmd == "register" and a.layer == "ortho":
            # fetched from the ortho service on first ask (ortho.py): nothing here to freeze it on
            if not a.live or not a.year:
                raise store_mod.StoreError("an ortho release is --live (it fetches from the ortho service as "
                                           "asked) and names the imagery's --year for its attribution")
            try:
                sources = {"upstream": ortho.upstream(), "year": a.year, "metadata": "CZ-CUZK-ORTOFOTO-R"}
            except ortho.OrthoError as e:
                raise store_mod.StoreError(e.detail)
            print(json.dumps(s.register(a.id, "ortho", sources, GENERATOR, "live", a.note).__dict__, indent=1))
        elif a.what == "release" and a.cmd == "register" and a.layer == "osm":
            # one archive that never changes: frozen, and its sha256 says which
            if a.live or not a.pmtiles:
                raise store_mod.StoreError("an osm release is one archive (--pmtiles <file>), frozen")
            try:
                sources = osm.describe(a.pmtiles)
            except osm.OsmError as e:
                raise store_mod.StoreError(str(e))
            print(json.dumps(s.register(a.id, "osm", sources, GENERATOR, "frozen", a.note).__dict__, indent=1))
        elif a.what == "release" and a.cmd == "register" and a.layer == "names":
            # one file that never changes: frozen, and its sha256 says which
            if a.live or not a.names:
                raise store_mod.StoreError("a names release is one file (--names <file>), frozen")
            try:
                sources = names.describe(a.names)
            except names.NamesError as e:
                raise store_mod.StoreError(str(e))
            print(json.dumps(s.register(a.id, "names", sources, names.GENERATOR, "frozen", a.note).__dict__, indent=1))
        elif a.what == "release" and a.cmd == "register" and a.layer == "models":
            # prepared areas, composed into one manifest that never changes: frozen, by sha256
            if a.live or not a.areas:
                raise store_mod.StoreError("a models release is prepared areas (--areas a,b), frozen")
            try:
                sources = citymodels.describe(a.areas.split(","))
            except citymodels.CityModelError as e:
                raise store_mod.StoreError(str(e))
            print(json.dumps(s.register(a.id, "models", sources, citymodels.GENERATOR, "frozen", a.note).__dict__, indent=1))
        elif a.what == "release" and a.cmd == "register":
            sources = {m: tiles.source_stamp(m) for m in tiles.MODELS}
            missing = sorted({k for m in sources.values() for k, v in m.items() if v is None})
            if missing and not a.live:
                print(f"warning: not on this machine: {', '.join(missing)} -- the release is registered "
                      f"on their absence, and the tiles that would read them fall back", file=sys.stderr)
            # checked against surveyed summits before anything can serve it (reference.py)
            if a.unchecked is not None:
                if not a.unchecked.strip():
                    raise store_mod.StoreError("--unchecked says why")
                sources["reference"] = {"unchecked": a.unchecked}
            else:
                try:
                    sources["reference"] = reference.check()
                except reference.ReferenceError as e:
                    raise store_mod.StoreError(f"not registered: {e}")
            r = s.register(a.id, a.layer, sources, GENERATOR, "live" if a.live else "frozen", a.note)
            print(json.dumps(r.__dict__, indent=1))
        elif a.what == "release":
            for r in s.releases():
                print(f"{r.id}\t{r.layer}\t{r.mode}\t{r.created_at}\t{r.note}")
        elif a.cmd == "set":
            bundle = {}
            for kv in a.releases:
                layer, _, rid = kv.partition("=")
                if not rid:
                    raise store_mod.StoreError(f"{kv!r}: give layer=release-id")
                bundle[layer] = rid
            print(json.dumps(s.set_channel(a.name, bundle, a.note), indent=1))
        else:
            for c in s.channels():
                print(f"{c['name']}\t{json.dumps(c['releases'], sort_keys=True)}\t{c['moved_at']}")
    except store_mod.StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
