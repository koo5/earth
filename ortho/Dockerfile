# ČÚZK orthophoto tile server: MapProxy (WMTS) over a MapServer 8 WMS source that reads
# the JPEG2000 sheets held on disk through a tile index, and a 4 m overview (README.md).
# Debian trixie (stable, security-patched GDAL 3.10 with JP2OpenJPEG, OpenJPEG 2.5.3) with MapServer from
# trixie-backports: 8.6.6 is the first without the WMS interpolation heap write (GHSA-59gr-4vvx-5f56), and
# trixie's own 8.4.0 has four CVEs open, "no DSA, postponed" (recon pl02). Ubuntu 24.04's 8.0.1 until 11-02.
# Cooloff: the backport was built 2026-09-10 and is pinned below, so a rebuild takes the same one until someone
# moves it. The base digest is a Debian stable point image pushed 2026-10-27, younger than the 14 days on
# purpose: a security refresh wants the latest point release (EARTH's review, 11-02).
FROM docker.io/library/debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie-backports\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/backports.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
      gdal-bin \
      python3 \
      python3-venv \
      ca-certificates \
      curl \
    && apt-get install -y --no-install-recommends -t trixie-backports \
      cgi-mapserver=8.6.6-1~bpo13+1 mapserver-bin=8.6.6-1~bpo13+1 libmapserver2t64=8.6.6-1~bpo13+1 \
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
# MapServer 8 loads a mapfile by path only when it matches this gate; the entrypoint writes the mapfile to
# config.GEN_DIR, a tmpfs, so the root filesystem can be read-only
RUN printf 'CONFIG\n  ENV\n    "MS_MAP_PATTERN" "^/run/ortho/.*\\.map$"\n  END\nEND\n' > /etc/mapserver.conf

WORKDIR /app
EXPOSE 8080
VOLUME ["/data", "/state", "/cache"]

# Healthy once MapProxy answers WMTS capabilities: nothing is built before serving (the
# overview is created in the background), so a start binds in seconds.
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS "http://localhost:8080/wmts/1.0.0/WMTSCapabilities.xml" >/dev/null || exit 1

ENTRYPOINT ["/opt/venv/bin/python", "/app/entrypoint.py"]
