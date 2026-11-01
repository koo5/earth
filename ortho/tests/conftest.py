"""Session fixtures: build the image, run one container whose sheet source (SHEETS_URL) is a local directory
holding the fixture sheet as ČÚZK ships it (<id>.zip), wait until it serves, tear everything down. The
container fetches the sheet itself when a detail tile first needs it.
CONTAINER_CLI=podman runs it under Podman."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import zipfile

import pytest

from helpers import (BASE, CLI, CONTAINER, FIXTURE_TILE, IMAGE, PORT, ROOT,
                     TILE_CENTER, deg2tile, http)


def _sh(*args):
    return subprocess.run(args, capture_output=True, text=True)


@pytest.fixture(scope="session")
def server():
    if not FIXTURE_TILE:
        pytest.skip("the container suite needs CUZK_FIXTURE_DIR: a directory holding one sheet's .jp2 + .j2w")

    build = _sh(CLI, "build", "-t", IMAGE, str(ROOT))
    assert build.returncode == 0, f"image build failed:\n{build.stderr[-2000:]}"

    work = pathlib.Path(tempfile.mkdtemp(prefix="cuzk-fix-"))
    src, data = work / "src", work / "data"
    src.mkdir()
    data.mkdir()
    # the sheet source: the .jp2 and its sidecars zipped as openzu ships them (the .j2w is the geotransform)
    with zipfile.ZipFile(src / f"{FIXTURE_TILE.stem}.zip", "w") as z:
        for ext in (".jp2", ".j2w", ".gml"):
            part = FIXTURE_TILE.with_suffix(ext)
            if part.exists():
                z.write(part, part.name)
    _sh(CLI, "rm", "-f", CONTAINER)
    run = _sh(CLI, "run", "-d", "--name", CONTAINER,
              "-p", f"{PORT}:8080",
              "-v", f"{src}:/src:ro",
              "-v", f"{data}:/data",
              "-e", "SHEETS_URL=file:///src",
              # the overview takes a sheet at once here, so the low-zoom test sees it
              "-e", "OV_MIN_SHEETS=1", "-e", "OV_QUIET_S=0", "-e", "MAINTAIN_S=2",
              IMAGE)
    assert run.returncode == 0, f"container start failed:\n{run.stderr}"

    try:
        ready = False
        deadline = time.time() + 180
        while time.time() < deadline:
            if _sh(CLI, "inspect", "-f", "{{.State.Running}}", CONTAINER).stdout.strip() == "false":
                break  # container exited (e.g. build error) — fail fast, don't wait out the timeout
            try:
                if http("/wmts/1.0.0/WMTSCapabilities.xml")[0] == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(3)
        if not ready:
            logs = _sh(CLI, "logs", CONTAINER)
            pytest.fail("server never became ready:\n" + (logs.stdout + logs.stderr)[-3000:])
        yield BASE
    finally:
        _sh(CLI, "rm", "-f", "-v", CONTAINER)
        # the container wrote /data as its own user (root in it): remove through it
        _sh(CLI, "run", "--rm", "-v", f"{work}:/w", "--entrypoint", "rm", IMAGE, "-rf", "/w/data")
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="session")
def fetched(server):
    """A detail tile asked once: the container fetches the fixture sheet."""
    x, y = deg2tile(*TILE_CENTER, 16)
    status, _, body = http(f"/wmts/oi/webmercator/16/{x}/{y}.png", timeout=120)
    assert status == 200, body[:300]
    return x, y


@pytest.fixture(scope="session")
def overview_ready(fetched):
    """Block until the overview has taken the fetched sheet (/status says nothing waits)."""
    import json
    deadline = time.time() + 300
    while time.time() < deadline:
        status, _, body = http("/status")
        if status == 200 and json.loads(body)["overview_pending"] == 0:
            return
        time.sleep(3)
    pytest.fail("the overview never took the fetched sheet")
