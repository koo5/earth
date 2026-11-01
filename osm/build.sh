#!/usr/bin/env bash
# Build earth's map layer: Geofabrik's extract -> planetiler (hillview-map.yml) -> one .pmtiles.
#
#   osm/build.sh <work dir> [area, default czech-republic]
#
# Writes <work dir>/<area>-<extract date>.pmtiles and prints it. The extract is downloaded into
# <work dir>/sources once (planetiler skips a present one; delete it for a newer OSM). Then copy the
# archive into the store's osm/ directory and register it (README: Publishing):
#   earth release register osm osm-cz-<date> --pmtiles <file name>
# Rootless Podman (earth.hillview.cz): keep-id maps the caller's uid in, so the archive is the caller's.
set -euo pipefail
work=$(realpath "${1:?work dir}"); area=${2:-czech-republic}
# planetiler v0.10.2, 2026-03-29: past the 14-day cooloff
image=ghcr.io/onthegomap/planetiler:0.10.2
here=$(dirname "$(realpath "$0")")
mkdir -p "$work/sources"
cp "$here/hillview-map.yml" "$work/hillview-map.yml"
out="$area-$(date -u +%Y-%m-%d).pmtiles"
podman run --rm --log-driver none --userns=keep-id --user "$(id -u):$(id -g)" -e JAVA_TOOL_OPTIONS="-Xmx3g" \
  -v "$work":/data "$image" generate-custom --schema=/data/hillview-map.yml --area="$area" \
  --download --output="/data/$out" --force >&2
echo "$work/$out"
