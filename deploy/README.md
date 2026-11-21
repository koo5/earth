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
    garage/     Garage's meta/ (sqlite, snapshots/) and data/ (blocks)
    sites/      Caddy sites the server serves besides earth's, one `<name>.caddy` each, not in this
                repository (`Caddyfile` imports them; an empty directory serves earth alone)

`~/earth/src/deploy/.env` (mode 600, never committed) holds the two path prefixes, Garage's secret and the
server's settings:

    EARTH_PREFIX=<8 random [a-z0-9]>          # earth's base, https://earth.hillview.cz/<prefix>/...
    EARTH_DEM_PREFIX=<openssl rand -hex 24>   # the terrain mosaics' own path, secret for good
    GARAGE_RPC_SECRET=<openssl rand -hex 32>  # Garage's node-to-node secret (and its CLI's)
    SHEET_BUDGET_GB=<GB>                      # the disk the ortho service's sheets may take; past it, no fetching

    python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)))'

EARTH_PREFIX is fixed and goes into the clients' config, so it is public to anyone who looks at a client.
What it keeps out is the scanners that find the host: anything outside it has its connection closed,
unanswered. It stays out of this repository, so a search there does not hand it out. Changing it breaks the
clients; if it ever must change, serve the old and new side by side until they have moved.

Deploy: `cd ~/earth/src && git pull --ff-only && cd deploy && podman compose up -d --build` (earth, ortho, Caddy
and Garage; the builder is a one-shot, `podman compose run --rm builder`). At boot `podman-restart.service`
(user, enabled) starts every container with `restart: always`. Unhealthy containers: `earth-heal.timer`
runs `heal-unhealthy.sh` every minute, which recreates through compose a container that is unhealthy, or whose
`earth.heal.probe` does not answer from the host (`cp earth-heal.service earth-heal.timer ~/.config/systemd/user/
&& systemctl --user daemon-reload && systemctl --user enable --now earth-heal.timer`);
`journalctl --user -u earth-heal` says what it did.

## Garage: an S3 store beside earth

`garage` (`compose.yml`, `garage.toml`) is an S3 object store for files other services keep on this server;
it is not earth's. Its S3 API (3900) and web endpoint (3902) listen on loopback only, on a network of their
own: nothing reaches them from outside but through a site the server adds to Caddy (`sites/`). One node, one
copy, and no backup made here. The CLI: `podman exec deploy-garage-1 /garage <cmd>` (`status`,
`bucket info <bucket>`, `key list`, `meta snapshot`). Set up once with `garage-setup.sh` (from this
directory): the layout, a bucket with its quota, and a key with read+write on it, written to a file you name.
