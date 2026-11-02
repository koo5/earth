#!/usr/bin/env python3
"""app/places.json: the towns whose orthophoto the ortho service downloads ahead when it is idle (app/prefetch.py).

    ./make_places.py <earth names release .json> [--min-population 20000]

From an earth names release (earth names build: OpenStreetMap's place nodes, ODbL, "© OpenStreetMap
contributors"): every city and town with a population tag of at least --min-population, largest first, as
{name, lat, lon, population}. Regenerate it when a new names release has better populations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("names")
    p.add_argument("--min-population", type=int, default=20000)
    a = p.parse_args()
    names = json.loads(Path(a.names).read_text())["names"]
    places = sorted(({"name": f["name"], "lat": f["lat"], "lon": f["lon"], "population": f["population"]}
                     for f in names if f["kind"] in ("city", "town") and f.get("population", 0) >= a.min_population),
                    key=lambda f: (-f["population"], f["name"]))
    out = Path(__file__).with_name("app") / "places.json"
    out.write_text(json.dumps({"source": f"{Path(a.names).name}: OpenStreetMap place nodes, ODbL, (c) OpenStreetMap "
                                         f"contributors; population >= {a.min_population}",
                               "places": places}, ensure_ascii=False, indent=0) + "\n")
    print(f"{out}: {len(places)} places")


if __name__ == "__main__":
    main()
