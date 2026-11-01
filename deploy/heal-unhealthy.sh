#!/bin/sh
# Restart earth's containers that are unhealthy and labelled earth.heal=restart. Rootless Podman has no
# daemon that acts on a failed healthcheck, so earth-heal.timer runs this every minute (deploy/README.md).
# 'Up' is not healthy.
set -eu
podman ps --filter health=unhealthy --filter label=earth.heal=restart --format '{{.Names}}' |
while read -r name; do
	echo "$name is unhealthy: restarting it" >&2
	podman restart "$name"
done
