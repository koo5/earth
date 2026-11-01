"""download_cuzk.py tests — the XML fixtures are verbatim excerpts of the
live atom.cuzk.gov.cz responses (captured 2026-07), so these pin the parsing
to the real service contract without any network."""
from pathlib import Path

import pytest

from download_cuzk import (Sheet, bbox_intersects, parse_dataset_feed,
                           parse_sheet_feed, polygon_bbox, want_download)

DATASET_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:georss="http://www.georss.org/georss"
      xmlns:inspire_dls="http://inspire.ec.europa.eu/schemas/inspire_dls/1.0"
      xmlns="http://www.w3.org/2005/Atom" xml:lang="cs">
  <id>https://atom.cuzk.cz/DMP1G-SJTSK/DMP1G-SJTSK.xml</id>
  <entry>
    <id>https://atom.cuzk.cz/DMP1G-SJTSK/datasetFeeds/CZ-00025712-CUZK_DMP1G-SJTSK_BENE09.xml</id>
    <title>DMP 1G - S-JTSK - mapovy list: Benesov 0-9</title>
    <link href="https://geoportal.cuzk.cz/SDIProCSW/service.svc/get?Id=X"
          title="metadata datasetu" type="application/xml" rel="describedby" />
    <link href="https://atom.cuzk.cz/DMP1G-SJTSK/datasetFeeds/CZ-00025712-CUZK_DMP1G-SJTSK_BENE09.xml"
          rel="alternate" title="dataset feed" type="application/atom+xml" />
    <category label="S-JTSK" term="http://www.opengis.net/def/crs/EPSG/0/5514" />
    <inspire_dls:spatial_dataset_identifier_code>CZ-00025712-CUZK_DMP1G-SJTSK_BENE09</inspire_dls:spatial_dataset_identifier_code>
    <georss:polygon>49.7764 14.6982 49.7764 14.7363 49.7972 14.7363 49.7972 14.6982 49.7764 14.6982</georss:polygon>
  </entry>
  <entry>
    <id>no-alternate-link-entry-must-be-skipped</id>
  </entry>
</feed>"""

SHEET_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:georss="http://www.georss.org/georss"
      xmlns:inspire_dls="http://inspire.ec.europa.eu/schemas/inspire_dls/1.0" xml:lang="cs">
   <id>https://atom.cuzk.gov.cz/X/datasetFeeds/Y.xml</id>
   <entry>
      <id>https://openzu.cuzk.gov.cz/opendata/X/epsg-3045/X-3045-20260706.zip</id>
      <link href="https://openzu.cuzk.gov.cz/opendata/X/epsg-3045/X-3045-20260706.zip"
            length="6732298971" rel="alternate" type="application/zip" hreflang="cs" />
      <category label="ETRS89/TM33" term="http://www.opengis.net/def/crs/EPSG/0/3045" />
   </entry>
   <entry>
      <id>https://openzu.cuzk.gov.cz/opendata/X/epsg-5514/X-5514-20260706.zip</id>
      <link href="https://openzu.cuzk.gov.cz/opendata/X/epsg-5514/X-5514-20260706.zip"
            length="5795632244" rel="alternate" type="application/zip" hreflang="cs" />
      <category label="S-JTSK" term="http://www.opengis.net/def/crs/EPSG/0/5514" />
   </entry>
</feed>"""


def test_parse_dataset_feed_real_shape():
    sheets = parse_dataset_feed(DATASET_FEED)
    assert len(sheets) == 1                      # malformed entry skipped
    s = sheets[0]
    assert s.code == "BENE09"
    assert s.feed_url.endswith("_BENE09.xml")
    lonmin, latmin, lonmax, latmax = s.bbox
    assert (lonmin, latmax) == (14.6982, 49.7972)
    assert latmin < latmax and lonmin < lonmax


def test_parse_sheet_feed_selects_epsg_5514():
    url, length = parse_sheet_feed(SHEET_FEED, "5514")
    assert "epsg-5514" in url and url.endswith(".zip")
    assert length == 5795632244


def test_parse_sheet_feed_other_crs_and_fallback():
    url, _ = parse_sheet_feed(SHEET_FEED, "3045")
    assert "epsg-3045" in url
    url, _ = parse_sheet_feed(SHEET_FEED, "9999")   # unknown → first entry
    assert "epsg-3045" in url
    with pytest.raises(ValueError):
        parse_sheet_feed("<feed xmlns='http://www.w3.org/2005/Atom'/>")


def test_polygon_bbox_lat_lon_order():
    # georss is "lat lon" pairs — the classic trap; bbox is lon-first
    assert polygon_bbox("50.0 14.0 50.2 14.5") == (14.0, 50.0, 14.5, 50.2)


def test_bbox_intersects():
    cz = (12.0, 48.5, 19.0, 51.1)
    assert bbox_intersects((14.69, 49.77, 14.74, 49.80), cz)
    assert not bbox_intersects((20.0, 49.0, 21.0, 50.0), cz)
    assert bbox_intersects((11.9, 48.4, 12.1, 48.6), cz)     # edge overlap


def test_load_index_refetches_poisoned_empty_cache(tmp_path: Path, monkeypatch):
    """An EMPTY cached index.json (artifact of a past bad fetch) must refetch
    instead of filtering every future bbox to zero forever — the Mělník
    on-demand build found '0 sheets' exactly this way (2026-07-28)."""
    import download_cuzk as dc
    (tmp_path / "index.json").write_text("[]")
    monkeypatch.setattr(dc, "http_get", lambda url: DATASET_FEED.encode())
    sheets = dc.load_index(tmp_path, "dmp1g", refresh=False)
    assert [s.code for s in sheets] == ["BENE09"]
    # and the refreshed (non-empty) index landed on disk
    assert "BENE09" in (tmp_path / "index.json").read_text()


def test_load_index_never_caches_an_empty_parse(tmp_path: Path, monkeypatch):
    import download_cuzk as dc
    empty = DATASET_FEED.split("<entry>")[0] + "</feed>"
    monkeypatch.setattr(dc, "http_get", lambda url: empty.encode())
    with pytest.raises(RuntimeError, match="0 sheets"):
        dc.load_index(tmp_path, "dmp1g", refresh=False)
    assert not (tmp_path / "index.json").exists()


def test_fetch_bbox_run_keeps_the_full_index(tmp_path: Path, monkeypatch):
    """A bbox-scoped fetch must not amputate index.json to its own subset —
    the Prague seed build did exactly that (16301 → 24 → 0 for the next
    bbox), which is how on-demand Mělník found '0 sheets'."""
    import argparse

    import download_cuzk as dc
    monkeypatch.setattr(dc, "http_get", lambda url: DATASET_FEED.encode())
    a = argparse.Namespace(out=tmp_path, dataset="dmp1g",
                           bbox="10.0,40.0,10.5,40.5",   # misses BENE09
                           limit=None, workers=1, epsg="5514",
                           unzip=False, sleep=0.0)
    dc.cmd_fetch(a)
    assert "BENE09" in (tmp_path / "index.json").read_text()


def test_want_download_skip_if_complete(tmp_path: Path):
    f = tmp_path / "a.zip"
    assert want_download(f, 100)                 # missing → download
    f.write_bytes(b"x" * 100)
    assert not want_download(f, 100)             # exact size → skip
    assert want_download(f, 200)                 # size mismatch → redo
    assert not want_download(f, None)            # exists, size unknown → keep


def test_count_prints_only_the_number(tmp_path: Path, monkeypatch, capsys):
    """auto_cuzk.sh reads `count` off stdout to decide "outside CZ"; the
    index fetch chatter must go to stderr or it breaks that test."""
    import download_cuzk as dc
    monkeypatch.setattr(dc, "http_get", lambda url: DATASET_FEED.encode())
    dc.main(["count", "--dataset", "dmp1g", "--out", str(tmp_path),
             "--bbox", "14.7,49.78,14.71,49.79"])
    assert capsys.readouterr().out == "1\n"
    dc.main(["count", "--dataset", "dmp1g", "--out", str(tmp_path),
             "--bbox", "10.0,40.0,10.5,40.5"])
    assert capsys.readouterr().out == "0\n"


def test_fetch_with_a_failed_sheet_exits_nonzero(tmp_path: Path, monkeypatch):
    """A failed sheet must stop auto_cuzk.sh (set -e) before it builds a
    mosaic with a hole, so fetch_cuzk reports an error, not "done"."""
    import argparse

    import download_cuzk as dc
    monkeypatch.setattr(dc, "http_get", lambda url: DATASET_FEED.encode())
    dc.load_index(tmp_path, "dmp1g", refresh=False)

    def boom(*a, **kw):
        raise IOError("sheet feed unreachable")
    monkeypatch.setattr(dc, "fetch_sheet", boom)
    a = argparse.Namespace(out=tmp_path, dataset="dmp1g", bbox=None, limit=None, workers=1,
                           epsg="5514", unzip=False, sleep=0.0)
    with pytest.raises(SystemExit) as e:
        dc.cmd_fetch(a)
    assert e.value.code == 1


def _zip_with(path: Path, member: str, data: bytes) -> int:
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, data)
    return path.stat().st_size


def test_an_unpacked_sheet_is_done_without_its_zip_and_without_asking(tmp_path: Path, monkeypatch):
    """Dropping zips must never make the next run download them again: the .laz is the record."""
    import download_cuzk as dc
    asked = []
    monkeypatch.setattr(dc, "http_get", lambda url: asked.append(url) or b"")
    (tmp_path / "laz").mkdir()
    (tmp_path / "laz" / "BENE09.laz").write_bytes(b"points")
    s = dc.Sheet(code="BENE09", feed_url="https://x/feed", bbox=(0, 0, 1, 1))
    assert "unpacked" in dc.fetch_sheet(s, tmp_path, "5514", unzip=True, sleep=0)
    assert asked == []


def test_unzip_drops_the_zip_once_unpacked_unless_kept(tmp_path: Path, monkeypatch):
    import download_cuzk as dc
    src = tmp_path / "src.zip"
    n = _zip_with(src, "BENE09.laz", b"points" * 100)
    monkeypatch.setattr(dc, "http_get", lambda url: src.read_bytes())
    s = dc.Sheet(code="BENE09", feed_url="f", bbox=(0, 0, 1, 1),
                 zip_url="https://x/BENE09.zip", zip_length=n)
    out = tmp_path / "out"
    dc.fetch_sheet(s, out, "5514", unzip=True, sleep=0)
    assert (out / "laz" / "BENE09.laz").read_bytes() == b"points" * 100
    assert not (out / "zips" / "BENE09.zip").exists()
    assert not list((out / "laz").glob("*.part"))
    s2 = dc.Sheet(code="BENE10", feed_url="f", bbox=(0, 0, 1, 1),
                  zip_url="https://x/BENE10.zip", zip_length=_zip_with(src, "BENE10.laz", b"p"))
    dc.fetch_sheet(s2, out, "5514", unzip=True, sleep=0, keep_zips=True)
    assert (out / "zips" / "BENE10.zip").exists() and (out / "laz" / "BENE10.laz").exists()
