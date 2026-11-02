#!/usr/bin/env bash
# Pull the named points the names layer is made from out of a Geofabrik extract (earth/names.py).
#
#   osm/names.sh <work dir> [area, default czech-republic]
#
# Reads <work dir>/sources/<area>.osm.pbf, the extract osm/build.sh downloaded (run that first), and writes
# <work dir>/<area>-names.geojsonseq: every node with a name and one of the tags the workbench's names query
# looks at, and water towers. The exact query (a tower only of an observation or communication type) is
# earth/names.py's in_query; the water towers are its water_tower.
# Then, where the mosaics are (the surface fills a missing ele):
#   earth names build <work dir>/<area>-names.geojsonseq --out <store>/names/names-<area>-<date>.json
#   earth release register names names-<area>-<date> --names names-<area>-<date>.json
set -euo pipefail
work=$(realpath "${1:?work dir}"); area=${2:-czech-republic}
pbf="$work/sources/$area.osm.pbf"
[ -f "$pbf" ] || { echo "no $pbf: run osm/build.sh $work $area first" >&2; exit 2; }
out="$area-names.geojsonseq"
# Debian's osmium-tool, from the distribution's archive (no third-party image); installed as the container's root,
# the export handed back to the caller
podman run --rm --log-driver none -v "$work":/data docker.io/library/debian:trixie-slim bash -c "
  set -euo pipefail
  apt-get update -qq && apt-get install -y -qq osmium-tool >/dev/null
  osmium tags-filter --overwrite -o /tmp/named.osm.pbf /data/sources/$area.osm.pbf \
    n/natural=peak n/man_made=tower,mast,water_tower n/place=city,town,village,suburb,quarter
  osmium export --overwrite -f geojsonseq -o /data/$out /tmp/named.osm.pbf
  chown $(id -u):$(id -g) /data/$out" >&2
echo "$work/$out"
