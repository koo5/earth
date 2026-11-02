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

import math
import os
import urllib.error
import urllib.request

# Czechia's box in EPSG:3857 (ČÚZK's ORTOFOTO_WM fullExtent); a tile wholly outside is not asked for
EXTENT_3857 = (1321579.96, 6141009.24, 2130407.22, 6694506.48)
# the source pixel is 12.5 cm; zoom 20 is 15 cm at the equator, ~10 cm here: finer is upsampling
MAX_Z = int(os.getenv("EARTH_ORTHO_MAX_Z", "20"))
TIMEOUT_S = float(os.getenv("EARTH_ORTHO_TIMEOUT_S", "20"))
USER_AGENT = os.getenv("EARTH_USER_AGENT", "earth.hillview.cz (https://hillview.cz)")

_HALF = 20037508.342789244


class OrthoError(RuntimeError):
    """Upstream failed or is not ready (not 'there is no imagery here', which is None): the status to answer,
    with the ortho service's Retry-After when it gave one (a tile whose sheets are downloading) and what the
    ask cost it (the downloads it started), which the caller still charges."""

    def __init__(self, status: int, detail: str, retry_after: str | None = None, cost: float = 0.0):
        super().__init__(detail)
        self.status, self.detail, self.retry_after, self.cost = status, detail, retry_after, cost


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
    """The upstream tile as (bytes, media type, what the ortho service says it cost), or None where it
    has none. OrthoError when asking failed, so the caller answers it. The caller holds the slots and
    charges the cost (app.py ortho_tile, limits.py)."""
    req = urllib.request.Request(upstream().format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body, cost = r.read(), r.headers.get("X-Ortho-Cost")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 503:
            # with Retry-After: its sheets are downloading, come back then (what that started is charged);
            # without: its sheet storage is full. Passed on as it said it.
            detail = e.read()[:300].decode(errors="replace")
            after = e.headers.get("Retry-After")
            if after is not None and not (after.isdigit() and 1 <= int(after) <= 600):
                raise OrthoError(502, f"the ortho service sent Retry-After {after!r} for ortho {z}/{x}/{y}")
            cost = e.headers.get("X-Ortho-Cost")
            raise OrthoError(503, f"orthophoto {z}/{x}/{y} unavailable: {detail}", after,
                             _cost(cost, z, x, y) if cost is not None else 0.0)
        raise OrthoError(502, f"the ortho service answered {e.code} for ortho {z}/{x}/{y}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OrthoError(503, f"the ortho service is unreachable for ortho {z}/{x}/{y}: "
                              f"{getattr(e, 'reason', e)}")
    price = _cost(cost, z, x, y)
    mt = media_type(body)
    if mt is None:
        raise OrthoError(502, f"the ortho service sent something not an image for ortho {z}/{x}/{y} "
                              f"({len(body)} B)")
    return body, mt, price


def _cost(header: str | None, z: int, x: int, y: int) -> float:
    """X-Ortho-Cost: a finite count, 0 or more; anything else is the ortho service's fault (502)."""
    try:
        cost = float(header) if header is not None else math.nan
    except ValueError:
        cost = math.nan
    if not math.isfinite(cost) or cost < 0:
        raise OrthoError(502, f"the ortho service did not say what ortho {z}/{x}/{y} cost "
                              f"(X-Ortho-Cost: {header!r})")
    return cost
