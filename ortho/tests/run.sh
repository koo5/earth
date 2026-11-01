#!/usr/bin/env bash
# Run the integration suite: builds the image, runs a container against a one-tile
# fixture, asserts the HTTP endpoints. Extra args pass through to pytest, e.g.
#   ./tests/run.sh -v
#   ./tests/run.sh -k lock
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --with pytest --with pillow --with pyproj pytest tests/ "$@"
