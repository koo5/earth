"""Releases, channels, kept tiles; the rate limiter."""
import pytest
from fastapi import HTTPException

from earth import limits, store


def test_a_release_never_changes_and_a_channel_points_at_releases(tmp_path):
    s = store.Store(str(tmp_path))
    r = s.register("terrain-2026-10-14a", "terrain", {"bare": {"x": [1, 2]}}, "gen/1")
    assert r.immutable and s.release(r.id) == r
    with pytest.raises(store.StoreError, match="exists"):
        s.register("terrain-2026-10-14a", "terrain", {}, "gen/1")
    with pytest.raises(store.StoreError, match="release id"):
        s.register("Terrain_1", "terrain", {}, "gen/1")
    s.register("osm-2026-10-13", "osm", {}, "gen/1")
    ch = s.set_channel("prod", {"terrain": "terrain-2026-10-14a"})
    assert ch["releases"] == {"terrain": "terrain-2026-10-14a"}
    # a move names only what changes; the rest stays
    assert s.set_channel("prod", {"osm": "osm-2026-10-13"})["releases"] == {
        "terrain": "terrain-2026-10-14a", "osm": "osm-2026-10-13"}
    with pytest.raises(store.StoreError, match="osm release"):
        s.set_channel("beta", {"terrain": "osm-2026-10-13"})
    with pytest.raises(store.StoreError, match="no release"):
        s.set_channel("beta", {"terrain": "nope"})
    assert [c["name"] for c in s.channels()] == ["prod"]


def test_tiles_are_kept_per_release_model_zoom_block_and_stamp(tmp_path):
    s = store.Store(str(tmp_path))
    s.put_tile("t1", "bare", 15, 17718, 11086, "q16", "abc", b"gz", {"X-Tile-N": "65"})
    assert s.get_tile("t1", "bare", 15, 17718, 11086, "q16", "abc") == (b"gz", {"X-Tile-N": "65"})
    assert s.get_tile("t1", "bare", 15, 17718, 11086, "q16", "other") is None
    assert s.get_tile("t1", "surface", 15, 17718, 11086, "q16", "abc") is None
    assert s.get_tile("t2", "bare", 15, 17718, 11086, "q16", "abc") is None
    # one pack per zoom-10 block: the tile's z10 ancestor names the file
    assert (tmp_path / "releases/t1/terrain/bare" / f"{17718 >> 5}-{11086 >> 5}.sqlite").exists()
    s.put_tile("t1", "bare", 3, 4, 2, "f32", "abc", b"low", {})
    assert (tmp_path / "releases/t1/terrain/bare/low.sqlite").exists()


def test_buckets_refill_at_their_rate_and_cap_at_their_burst():
    now = [0.0]
    b = limits.Buckets(rate=2, burst=3, clock=lambda: now[0])
    assert [b.take("a") for _ in range(3)] == [0, 0, 0]
    assert b.take("a") == pytest.approx(0.5)          # empty: half a second to the next token
    assert b.take("b") == 0                           # another client has its own
    now[0] = 10.0
    assert [b.take("a") for _ in range(4)][-1] > 0    # refilled to the burst, not beyond


def test_buckets_forget_the_least_recent_client():
    b = limits.Buckets(rate=1, burst=1, max_keys=2, clock=lambda: 0.0)
    b.take("a"), b.take("b"), b.take("c")
    assert b.take("a") == 0                           # forgotten, so full again: never punishing


class Req:
    def __init__(self, peer, fwd=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": fwd} if fwd else {}


def test_the_client_address_believes_only_the_proxy_in_front(monkeypatch):
    monkeypatch.setenv("EARTH_TRUSTED_PROXIES", "10.0.0.2")
    assert limits.client_ip(Req("10.0.0.2", "203.0.113.7")) == "203.0.113.7"
    assert limits.client_ip(Req("198.51.100.1", "203.0.113.7")) == "198.51.100.1"   # not our proxy
    with pytest.raises(HTTPException) as e:
        limits.client_ip(Req("10.0.0.2", "1.2.3.4, 203.0.113.7"))                   # injected
    assert e.value.status_code == 403
