#!/bin/sh
# Everything earth's terrain reads, built into /dem from the sources. Every stage is incremental, so a
# rerun resumes.
#
#   build_all.sh [cuzk-bbox] [layer]      default: all of Czechia, both models
#
# - ČÚZK DMR 5G (bare) and DMP 1G (surface): auto_cuzk.sh -> /dem/cuzk/{dl,rast,warp}, dtm/dsm 2 m + 10 m VRTs
# - Copernicus GLO-30 over central Europe -> /dem/glo30 + /dem/glo30.vrt (the far ring, across the border)
# - ETOPO 2022 60" (the whole earth, the fill under everything) -> /dem/global
# - EGM2008 2.5' geoid (ellipsoidal <-> orthometric) -> /dem/geoid
set -eu
cd "$(dirname "$0")"
cz="${1:-12.09,48.55,18.87,51.06}"
layer="${2:-both}"
glo30="${GLO30_BBOX:-5,44,25,56}"

fetch() {   # url dest sha256: dest exists only when complete and verified; a partial .tmp is resumed
    [ -s "$2" ] && return 0
    mkdir -p "$(dirname "$2")"
    curl -fL --retry 5 --retry-delay 10 -C - -o "$2.tmp" "$1"
    echo "$3  $2.tmp" | sha256sum -c -
    mv -f "$2.tmp" "$2"
}

echo "build_all: geoid + ETOPO"
# pinned by sha256
fetch https://cdn.proj.org/us_nga_egm08_25.tif /dem/geoid/us_nga_egm08_25.tif \
      4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a
fetch https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO2022/data/60s/60s_surface_elev_gtif/ETOPO_2022_v1_60s_N90W180_surface.tif \
      /dem/global/ETOPO_2022_v1_60s_N90W180_surface.tif \
      9d27d4b8ea8e76977e2988bca667d7c8fa68b927355feffcddd6b4875a7fd08e

echo "build_all: GLO-30 over $glo30"
python download_glo30.py --bbox "$glo30" --out /dem/glo30
gdalbuildvrt -resolution highest /dem/glo30.vrt.tmp /dem/glo30/*.tif && mv -f /dem/glo30.vrt.tmp /dem/glo30.vrt

echo "build_all: ČÚZK $layer over $cz"
sh auto_cuzk.sh "$cz" "$layer"
echo "build_all: done"
