# earth's terrain builder (builder/: download_cuzk.py, build_mosaic.py, auto_cuzk.sh, download_glo30.py),
# run as a one-shot compose service. The base is a pinned
# PDAL image (pdal 2.10.2, GDAL 3.13.3 gdalwarp/gdalbuildvrt, python 3.14: the scripts are stdlib-only);
# flock (util-linux) is the one thing it lacks, for auto_cuzk.sh's build lock.
FROM docker.io/pdal/pdal@sha256:8e0eb32d54e92058e0e73a6a5ce8cd9ac3a59d30f07c6827e6d7cb7a2e392883
RUN apt-get update && apt-get install -y --no-install-recommends util-linux ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /builder
COPY builder/ ./
ENTRYPOINT ["sh", "/builder/build_all.sh"]
