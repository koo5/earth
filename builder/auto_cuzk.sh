#!/bin/sh
# Bbox-scoped ČÚZK near-ring build into /dem/cuzk — the containerized twin of
# the manual README flow. DMP 1G (surface → DSM rays) + DMR 5G (bare earth →
# DTM grounding), rasterized at 2 m (pdal), warped to the 10 m EPSG:4326 near
# ring, assembled into one VRT per raster.
#
#   auto_cuzk.sh <lonmin,latmin,lonmax,latmax> [dtm|dsm|both]
#
# The layer (default both, what a render needs) picks which half is built:
# dtm = DMR 5G → dtm10.vrt (bare earth: grounding, the API's want="bare");
# dsm = DMP 1G → dsm10.vrt + dsm2.vrt (surface: what the rays march). A
# ground fetch (worker fetch_cuzk) asks for only the half it lacks — DMP 1G
# is the heavier download and bare-earth callers never read it.
#
# Every stage is INCREMENTAL: downloads skip complete files, rasterize/warp
# skip existing outputs, the cheap VRT rebuild picks up whatever is present.
# So the tree only ever grows — a bigger bbox later (or all of Czechia:
# build_all.sh) just adds sheets. Disk note: laz + 2 m rasters + 10 m warps stay in the volume
# (that's the resumability); a zip is dropped once its .laz is unpacked, and an
# unpacked .laz counts as downloaded (download_cuzk.py, 2026-10-02).
#
# VRTs are written to a temp name and renamed over the old one, as the GLO-30
# VRT is: a reader (the API samples dtm10.vrt on every run page) sees the old
# mosaic or the new one, never a half-written file.
set -eu

bbox="$1"
layer="${2:-both}"
# sheets rasterized/warped at once (build_mosaic.py --jobs; compose.yml sets it)
jobs="${BUILD_JOBS:-1}"
case "$layer" in
    dtm|dsm|both) ;;
    *) echo "auto_cuzk: layer must be dtm, dsm or both, not '$layer'" >&2; exit 2 ;;
esac
base=/dem/cuzk
mkdir -p "$base"
# Serialize concurrent builds — container start, the worker's on-demand
# fetches, and manual prefetch runs may all share the volume. The lock lives
# HERE (not in callers) so every path gets it and none can deadlock a child.
exec 9>"$base/.build.lock"
flock 9
echo "auto_cuzk: ČÚZK near ring ($layer) for bbox $bbox → $base (incremental)"

# rebuild one VRT from a warp dir, atomically (tmp + rename)
vrt() {
    python build_mosaic.py vrt --in-dirs "$base/warp/$1" --out "$base/$1.vrt.tmp"
    mv -f "$base/$1.vrt.tmp" "$base/$1.vrt"
}

# Is the bbox inside ČÚZK coverage at all? Asked of the sheet INDEX for this
# bbox, not of the laz directory: that directory is non-empty forever after the
# first build, so the old `ls laz/*` test could never say "outside" again.
ds=dmr5g; [ "$layer" = dsm ] && ds=dmp1g
n="$(python download_cuzk.py count --dataset "$ds" --out "$base/dl/$ds" --bbox "$bbox")"
if [ "$n" = 0 ]; then
    echo "auto_cuzk: no ČÚZK sheet intersects bbox $bbox (outside CZ coverage) — nothing to build"
    exit 0
fi

if [ "$layer" != dsm ]; then
    python download_cuzk.py fetch --dataset dmr5g --out "$base/dl/dmr5g" --bbox "$bbox" --unzip
    python build_mosaic.py rasterize --jobs "$jobs" --laz-dir "$base/dl/dmr5g/laz" --out-dir "$base/rast/dtm" --res-m 2 --kind dtm
    python build_mosaic.py warp --jobs "$jobs" --in-dir "$base/rast/dtm" --out-dir "$base/warp/dtm10" --res-m 10 --s-srs "EPSG:5514+8357"
    # 2 m bare earth: the 3D world's fine terrain tiles (zoom 16-18, api routers/terrain.py);
    # the renderer reads only the 2 m SURFACE
    python build_mosaic.py warp --jobs "$jobs" --in-dir "$base/rast/dtm" --out-dir "$base/warp/dtm2" --res-m 2 --s-srs "EPSG:5514+8357"
    vrt dtm2
    vrt dtm10
fi
if [ "$layer" != dtm ]; then
    python download_cuzk.py fetch --dataset dmp1g --out "$base/dl/dmp1g" --bbox "$bbox" --unzip
    python build_mosaic.py rasterize --jobs "$jobs" --laz-dir "$base/dl/dmp1g/laz" --out-dir "$base/rast/dsm" --res-m 2 --kind dsm
    python build_mosaic.py warp --jobs "$jobs" --in-dir "$base/rast/dsm" --out-dir "$base/warp/dsm10" --res-m 10 --s-srs "EPSG:5514+8357"
    # 2 m near ring: native-ish lidar sharpness for the first few km — nearby
    # trees/buildings render as shapes instead of interpolated 10 m slabs
    python build_mosaic.py warp --jobs "$jobs" --in-dir "$base/rast/dsm" --out-dir "$base/warp/dsm2" --res-m 2 --s-srs "EPSG:5514+8357"
    vrt dsm2
    vrt dsm10
fi
echo "auto_cuzk: done ($layer) — $base"
