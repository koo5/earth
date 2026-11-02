#!/bin/sh
# Recreate earth's containers (labelled earth.heal=restart) that are unhealthy, or whose earth.heal.probe URL
# does not answer from the host. Rootless Podman has no daemon that acts on a failed healthcheck, so
# earth-heal.timer runs this every minute (deploy/README.md). 'Up' is not healthy, and healthy inside is not
# reachable: on 2026-11-02 a `podman restart` of a busy earth (SIGKILLed with Caddy's connections open) came
# back healthy with nothing listening on its published 127.0.0.1:8090, and Caddy answered 502 for 20 minutes.
# So a container is recreated through compose (fresh networking and port forwarder), never restarted, and the
# probe from the host catches that state whatever causes it. A container still starting is left alone.
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"            # docker-compose, podman compose's provider
podman ps --filter label=earth.heal=restart --format '{{.Names}}' |
while read -r name; do
	health=$(podman inspect -f '{{.State.Health.Status}}' "$name" </dev/null)
	probe=$(podman inspect -f '{{index .Config.Labels "earth.heal.probe"}}' "$name" </dev/null)
	why=
	if [ "$health" = unhealthy ]; then
		why="unhealthy"
	elif [ "$health" = healthy ] && [ -n "$probe" ] && [ "$probe" != "<no value>" ] \
			&& ! curl -fsS -m 10 -o /dev/null "$probe" </dev/null; then
		why="healthy inside, but $probe does not answer from the host"
	fi
	[ -n "$why" ] || continue
	service=$(podman inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$name" </dev/null)
	echo "$name: $why: recreating $service" >&2
	podman compose up -d --force-recreate --no-build "$service" </dev/null
done
