#!/usr/bin/env python3
"""The list of ČÚZK orthophoto sheets that exist, from ČÚZK's INSPIRE Atom service feed.

    python3 make_sheets.py [feed.xml] > app/sheets.txt     # no argument: download https://atom.cuzk.gov.cz/OI/OI.xml

One line per sheet, sorted: "<E_km>_<N_km> <updated>". A sheet id is its lower-left corner in ETRS89/TM33
(EPSG:3045, kilometres): sheet 602_5554 covers E 602000-604000, N 5554000-5556000 at 0.125 m (16000 x 16000).
The service computes the sheets under a tile from that; this list says which exist and when ČÚZK last updated
each. Stdlib only.
"""
import re
import sys
import urllib.request

FEED = "https://atom.cuzk.gov.cz/OI/OI.xml"
ENTRY = re.compile(r"<entry>.*?CUZK_OI_(\d+)_(\d+)\.xml</id>.*?<updated>([^<]+)</updated>", re.S)


def main() -> int:
    if len(sys.argv) > 1:
        text = open(sys.argv[1], encoding="utf-8").read()
    else:
        with urllib.request.urlopen(FEED, timeout=120) as r:
            text = r.read().decode("utf-8")
    sheets = sorted({(int(e), int(n), updated) for e, n, updated in ENTRY.findall(text)})
    if len(sheets) < 15000:
        raise SystemExit(f"only {len(sheets)} sheets in the feed: not the whole of Czechia, refusing")
    for e, n, updated in sheets:
        print(f"{e}_{n} {updated}")
    print(f"{len(sheets)} sheets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
