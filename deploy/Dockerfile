# The earth service. Build context: the repo root.
# fully qualified: rootless Podman (earth's own server) resolves no short names
FROM docker.io/library/python:3.12-slim
# rasterio's wheels bundle GDAL but link the system's expat, which -slim does not have
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY earth ./earth
RUN uv sync --frozen --no-dev
# fail the build, not the first tile, when a native library is missing
RUN .venv/bin/python -c "import rasterio, pyproj, numpy; import earth.app"
ENV PATH="/app/.venv/bin:$PATH" EARTH_STORE=/var/lib/earth
# one process: the rate limits' state lives in it (limits.py)
# no --proxy-headers: which X-Forwarded-For to believe is limits.client_ip's decision, not uvicorn's
CMD ["uvicorn", "earth.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
