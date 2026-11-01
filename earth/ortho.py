"""The orthophoto: ČÚZK Ortofoto ČR from the stack's ortho service, passed on as it came, never kept.

ČÚZK publishes it as open data under CC BY 4.0 since 2023-07-01 (metadata record
CZ-CUZK-ORTOFOTO-R: "Licence Creative Commons CC BY 4.0", "Bez poplatků"), 12.5 cm pixels,
renewed in a two-year cycle, half the country a year. The upstream is the stack's ortho service
(../ortho, EARTH_ORTHO_URL), which renders it from ČÚZK's JPEG2000 sheets as it is asked: the same
slippy z/x/y as the terrain tiles, so a terrain tile drapes the ortho tile of the same address.
The terms (cuzk.gov.cz, Podmínky poskytování prostorových dat ČÚZK) ask that a viewing service
names the source as "ČÚZK, <year>", links to the terms, and travels with the metadata;
attribution.py carries that. The bytes go out as the ortho service sent them (JPEG, PNG where it
holds no sheet): reprojected and cut into tiles, the imagery itself not modified. The ortho service
says what each tile cost (X-Ortho-Cost: sheets fetched, a tile drawn); earth charges it (limits.py).

Czechia only for now: a tile outside the service's extent is a 404 without asking upstream.
An ortho release is `live`: the imagery changes as ČÚZK renews it; the year it shows is the release's
own (`sources.year`).
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request

# Czechia's box in EPSG:3857 (ČÚZK's ORTOFOTO_WM fullExtent); a tile wholly outside is not asked for
EXTENT_3857 = (1321579.96, 6141009.24, 2130407.22, 6694506.48)
# the source pixel is 12.5 cm; zoom 20 is 15 cm at the equator, ~10 cm here: finer is upsampling
MAX_Z = int(os.getenv("EARTH_ORTHO_MAX_Z", "20"))
TIMEOUT_S = float(os.getenv("EARTH_ORTHO_TIMEOUT_S", "20"))
USER_AGENT = os.getenv("EARTH_USER_AGENT", "earth.hillview.cz (https://hillview.cz)")
# how many tiles may be asked of the ortho service at once, whoever asks: as many as it has workers
# (its WORKERS); more would only queue there
_upstream = threading.Semaphore(int(os.getenv("EARTH_ORTHO_UPSTREAM_CONCURRENT", "4")))

_HALF = 20037508.342789244


class OrthoError(RuntimeError):
    """Upstream failed (not 'there is no imagery here', which is None): the status to answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def tile_extent_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * _HALF / (1 << z)
    return (-_HALF + x * size, _HALF - (y + 1) * size, -_HALF + (x + 1) * size, _HALF - y * size)


def covered(z: int, x: int, y: int) -> bool:
    """Whether the tile touches the service's extent at all."""
    w, s, e, n = tile_extent_3857(z, x, y)
    W, S, E, N = EXTENT_3857
    return not (e <= W or E <= w or n <= S or N <= s)


def media_type(body: bytes) -> str | None:
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def upstream() -> str:
    """The ortho service's tile URL template, {z}/{x}/{y} (deploy/compose.yml sets it); no default."""
    url = os.getenv("EARTH_ORTHO_URL")
    if not url:
        raise OrthoError(503, "EARTH_ORTHO_URL is not set: earth has no orthophoto upstream")
    return url


def fetch(z: int, x: int, y: int) -> tuple[bytes, str, float] | None:
    """The upstream tile as (bytes, media type, what it cost in tiles made), or None where it has
    none. OrthoError when asking failed, so the caller answers it."""
    req = urllib.request.Request(upstream().format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
    with _upstream:
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body, cost = r.read(), r.headers.get("X-Ortho-Cost")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 503:                       # its sheet storage is full: passed on as it said it
                detail = e.read()[:300].decode(errors="replace")
                raise OrthoError(503, f"orthophoto {z}/{x}/{y} unavailable: {detail}")
            raise OrthoError(502, f"the ortho service answered {e.code} for ortho {z}/{x}/{y}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OrthoError(503, f"the ortho service is unreachable for ortho {z}/{x}/{y}: "
                                  f"{getattr(e, 'reason', e)}")
    mt = media_type(body)
    if mt is None:
        raise OrthoError(502, f"the ortho service sent something not an image for ortho {z}/{x}/{y} "
                              f"({len(body)} B)")
    if cost is None:
        raise OrthoError(502, f"the ortho service did not say what ortho {z}/{x}/{y} cost (X-Ortho-Cost)")
    return body, mt, float(cost)
