# Deploying earth

earth.hillview.cz runs `compose.yml` on rootless Podman through `podman compose` (docker compose is
its provider, set in `~/.config/containers/containers.conf`). Two root steps first: `loginctl
enable-linger <user>` (the containers outlive a login) and `net.ipv4.ip_unprivileged_port_start=80`
(Caddy binds 80/443).

The layout, under `~/earth/` (earth-heal.service runs the checkout's script from there; compose.yml
names the data relative to the checkout):

    src/        this repository
    dem/        terrain sources and mosaics (the builder writes them; read-only to earth)
    store/      releases, channels, kept tiles, osm/, models/, grids/
    ortho/      the ortho service: sheets/ (JP2, fetched as needed), state/ (index, overview), cache/

`~/earth/src/deploy/.env` (mode 600, never committed) holds the two path prefixes:

    EARTH_PREFIX=<8 random [a-z0-9]>          # earth's base, https://earth.hillview.cz/<prefix>/...
    EARTH_DEM_PREFIX=<openssl rand -hex 24>   # the terrain mosaics' own path, secret for good

    python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)))'

EARTH_PREFIX is fixed and goes into the clients' config, so it is public to anyone who looks at a client.
What it keeps out is the scanners that find the host: anything outside it has its connection closed,
unanswered. It stays out of this repository, so a search there does not hand it out. Changing it breaks the
clients; if it ever must change, serve the old and new side by side until they have moved.

Deploy: `cd ~/earth/src && git pull --ff-only && cd deploy && podman compose up -d --build` (earth, ortho and
Caddy; the builder is a one-shot, `podman compose run --rm builder`). At boot `podman-restart.service`
(user, enabled) starts every container with `restart: always`. Unhealthy containers: `earth-heal.timer`
runs `heal-unhealthy.sh` every minute (`cp earth-heal.service earth-heal.timer ~/.config/systemd/user/
&& systemctl --user daemon-reload && systemctl --user enable --now earth-heal.timer`);
`journalctl --user -u earth-heal` says what it did.

While 80/443 are closed, the same site answers in plain HTTP on the server's loopback, port 8081.
