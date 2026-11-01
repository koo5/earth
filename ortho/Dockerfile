# ČÚZK orthophoto tile server: MapProxy (WMTS) over a MapServer 8 WMS source that reads
# the JPEG2000 sheets held on disk through a tile index, and a 4 m overview (README.md).
# Ubuntu 24.04 == the validated host env (MapServer 8, GDAL w/ JP2OpenJPEG).
FROM docker.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      gdal-bin \
      cgi-mapserver \
      python3 \
      python3-venv \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

# MapProxy + gunicorn in an isolated venv (avoids PEP 668 / system-managed pip).
# Fully pinned with hashes: requirements.txt = uv pip compile --generate-hashes.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

# Runtime knobs (all overridable with `docker run -e`; config.py says what each does). Sheets arrive on
# demand from SHEETS_URL into /data: nothing to rebuild or refresh by hand.
ENV DATA_DIR=/data \
    STATE_DIR=/state \
    CACHE_DIR=/cache \
    SRS=EPSG:25833 \
    OV_RES=4 \
    CROSSOVER=16000 \
    BUILD_LOG_INTERVAL=15 \
    WORKERS=4

COPY app/ /app/

WORKDIR /app
EXPOSE 8080
VOLUME ["/data", "/state", "/cache"]

# Healthy once MapProxy answers WMTS capabilities: nothing is built before serving (the
# overview is created in the background), so a start binds in seconds.
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS "http://localhost:8080/wmts/1.0.0/WMTSCapabilities.xml" >/dev/null || exit 1

ENTRYPOINT ["/opt/venv/bin/python", "/app/entrypoint.py"]
