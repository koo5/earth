"""The Python half of the geodesy, pinned to the browser's by the cases both test against
(hillview_common world/geodesy-cases.json, copied here)."""
import json
from pathlib import Path

import numpy as np
import pytest

from earth import geodesy as g

HERE = Path(__file__).resolve().parent


def test_the_cases_are_this_arithmetic():
    for f in json.loads((HERE / "geodesy-cases.json").read_text())["frames"]:
        w = g.Frame(*f["origin"])
        geo = np.asarray(f["geodetic"], float)
        assert np.abs(w.from_geodetic(geo[:, 0], geo[:, 1], geo[:, 2]) - np.asarray(f["enu"])).max() < 1e-6


def test_the_copy_is_hillview_common_s():
    theirs = HERE.parents[1] / "hillview_common" / "world" / "geodesy-cases.json"
    if not theirs.exists():
        pytest.skip("hillview_common is not checked out beside this repo")
    assert json.loads(theirs.read_text())["frames"] == json.loads((HERE / "geodesy-cases.json").read_text())["frames"]
