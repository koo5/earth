"""The orthophoto: asked of the ortho service every time (faked here at ortho.fetch), never kept, what it
cost charged to the make bucket, 404 outside Czechia without asking, and its attribution with the year."""
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

from earth import app as app_mod, limits, ortho, store
from conftest import tile_of

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
CHURCH = tile_of(50.18723, 14.661822, 18)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTH_NO_LIMITS", "1")
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    asked = []

    def fetch(z, x, y):
        asked.append((z, x, y))
        return None if (z, x, y) == (18, 1, 1) else (JPEG, "image/jpeg", 0.0)
    monkeypatch.setattr(ortho, "fetch", fetch)
    s = store.Store()
    s.register("ortho-a", "ortho", {"year": "2024-2025"}, "test", mode="live")
    return TestClient(app_mod.create_app(store=s, limits=limits.Limits())), asked


def test_a_tile_is_passed_on_as_it_came_and_never_kept(svc):
    c, asked = svc
    z, x, y = CHURCH
    for _ in range(2):
        r = c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg")
        assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
        assert r.headers["x-attribution"] == "cuzk-ortho" and r.headers["cache-control"] == app_mod.LIVE
    assert asked == [CHURCH, CHURCH]                               # the ortho service caches, not earth


class Upstream:
    """urlopen faked: the ortho service answering JPEG with an X-Ortho-Cost, optionally held at a gate."""

    def __init__(self, costs, gate=None):
        self.costs, self.gate, self.entered = costs, gate, threading.Event()

    def __call__(self, req, timeout):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        z, x, y = (int(v) for v in req.full_url.rsplit("/", 3)[1:])
        return io.BytesIO(JPEG), self.costs.get((z, x, y), "0")


@pytest.fixture
def limited(tmp_path, monkeypatch):
    """The app with limits on (make burst 3, no refill to speak of), one ortho slot per address and two in
    all, urlopen faked. An address is X-Forwarded-For from the test client, which is made the trusted proxy."""
    monkeypatch.delenv("EARTH_NO_LIMITS", raising=False)
    monkeypatch.setenv("EARTH_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("EARTH_ORTHO_URL", "http://ortho/{z}/{x}/{y}")
    monkeypatch.setenv("EARTH_LIMIT_MAKE_RATE", "0.001")
    monkeypatch.setenv("EARTH_LIMIT_MAKE_BURST", "3")
    monkeypatch.setenv("EARTH_LIMIT_ORTHO_PER_ADDRESS", "1")
    monkeypatch.setenv("EARTH_LIMIT_ORTHO_CONCURRENT", "2")
    monkeypatch.setenv("EARTH_TRUSTED_PROXIES", "testclient")
    s = store.Store()
    s.register("ortho-a", "ortho", {"year": "2024-2025"}, "test", mode="live")

    def use(upstream):
        class Response:
            def __init__(self, req, timeout):
                self.body, cost = upstream(req, timeout)
                self.headers = {"X-Ortho-Cost": cost}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body.read()
        monkeypatch.setattr(ortho.urllib.request, "urlopen", Response)
        # entered: its requests share one event loop, as uvicorn's do (the ortho slots are asyncio
        # semaphores; outside the context each request would run on a loop of its own)
        c = TestClient(app_mod.create_app(store=s, limits=limits.Limits()))
        clients.append(c.__enter__())
        return c
    clients: list = []
    yield use
    for c in clients:
        c.__exit__(None, None, None)


CHEAP, DEAR = tile_of(50.1, 14.4, 17), tile_of(50.2, 14.5, 17)


def url(t):
    return "/t/ortho-a/ortho/{}/{}/{}.jpg".format(*t)


def test_what_a_tile_cost_is_charged_and_an_address_in_debt_waits(limited):
    c = limited(Upstream({DEAR: "5"}))
    for _ in range(10):                                            # cached tiles cost nothing made
        assert c.get(url(CHEAP)).status_code == 200
    assert c.get(url(DEAR)).status_code == 200                     # 3 - 5: in debt
    r = c.get(url(CHEAP))
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 1


def test_a_second_dear_tile_in_flight_is_refused_once_the_first_is_charged(limited):
    gate = threading.Event()
    up = Upstream({DEAR: "5", (DEAR[0], DEAR[1] + 1, DEAR[2]): "5"}, gate)
    c = limited(up)
    first = threading.Thread(target=lambda: got.append(c.get(url(DEAR)).status_code))
    got: list = []
    first.start()
    assert up.entered.wait(5)                                      # the first holds the address's one slot
    second = threading.Thread(target=lambda: got.append(c.get(url((DEAR[0], DEAR[1] + 1, DEAR[2]))).status_code))
    second.start()
    time.sleep(0.3)                                                # it is in, waiting for the slot (or past a check)
    gate.set()
    first.join(5)
    second.join(5)
    assert sorted(got) == [200, 429]


def test_one_address_waiting_on_new_sheets_does_not_hold_another_address_cached_tile(limited):
    """A viewer's new area (tiles waiting minutes for their sheets) held every other viewer's cached tiles
    behind one shared cap of 4: 170 s on earth.hillview.cz, 2026-11-02 (recon pl03)."""
    gate = threading.Event()
    up = Upstream({}, gate)
    c = limited(up)
    slow: list = []
    a = threading.Thread(target=lambda: slow.append(c.get(url(DEAR), headers={"X-Forwarded-For": "10.0.0.1"})))
    a.start()
    assert up.entered.wait(5)                                      # 10.0.0.1 holds its slot, waiting upstream
    up.gate = None                                                 # what follows answers at once
    t = time.monotonic()
    r = c.get(url(CHEAP), headers={"X-Forwarded-For": "10.0.0.2"})
    assert r.status_code == 200 and time.monotonic() - t < 2 and not slow
    gate.set()
    a.join(5)
    assert slow[0].status_code == 200


class Downloading(Upstream):
    """The ortho service answering a tile whose sheets are downloading: 503 at once, Retry-After, what it started."""

    def __init__(self, after="30", started="2"):
        super().__init__({})
        self.after, self.started = after, started

    def __call__(self, req, timeout):
        import email.message
        import urllib.error
        h = email.message.Message()
        h["Retry-After"], h["X-Ortho-Cost"] = self.after, self.started
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", h,
                                     io.BytesIO(b"its sheets are downloading; retry in 30 s"))


def test_a_tile_whose_sheets_are_downloading_is_passed_on_as_503_with_retry_after_and_charged(limited):
    c = limited(Downloading())
    r = c.get(url(DEAR))
    assert r.status_code == 503 and r.headers["retry-after"] == "30" and "downloading" in r.json()["detail"]
    assert c.get(url(DEAR)).status_code == 503                    # 3 - 2 - 2: what it started is charged
    r = c.get(url(DEAR))
    assert r.status_code == 429                                    # an address in debt waits
    assert limited(Downloading(after="soon")).get(url(CHEAP)).status_code == 502


def test_a_cost_that_is_not_a_finite_count_is_the_upstreams_fault(limited):
    for bad in ("x", "inf", "-1", "nan"):
        assert limited(Upstream({CHEAP: bad})).get(url(CHEAP)).status_code == 502


def test_outside_czechia_and_where_cuzk_has_none_is_404_and_not_kept(svc):
    c, asked = svc
    z, x, y = tile_of(48.85, 2.35, 18)                         # Paris: never asked upstream
    assert c.get(f"/t/ortho-a/ortho/{z}/{x}/{y}.jpg").status_code == 404 and asked == []
    assert c.get("/t/ortho-a/ortho/18/1/1.jpg").status_code == 404     # wholly outside too
    assert asked == []
    assert c.get("/t/nope/ortho/18/1/1.jpg").status_code == 404
    assert c.get(f"/t/ortho-a/ortho/{ortho.MAX_Z + 1}/0/0.jpg").status_code == 404


def test_the_attribution_names_cuzk_with_the_terms_and_the_metadata(svc):
    c, _ = svc
    a = c.get("/attribution").json()["sources"]["cuzk-ortho"]
    assert a["text"] == "ČÚZK, {year}" and a["licence"] == "CC BY 4.0"
    assert "Podminky" in a["terms_url"] and "CZ-CUZK-ORTOFOTO-R" in a["metadata_url"]
    assert c.get("/releases/ortho-a").json()["sources"]["year"] == "2024-2025"


def test_coverage_is_the_service_extent():
    assert ortho.covered(*tile_of(50.08, 14.42, 15)) and ortho.covered(*tile_of(49.0, 17.0, 12))
    assert not ortho.covered(*tile_of(48.85, 2.35, 10))
