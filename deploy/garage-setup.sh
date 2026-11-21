#!/bin/bash
# Garage set up once (README.md "Garage"): the one node's layout, a bucket with its quota, and a key with read+write
# on it. From this directory, with the garage service up:
#
#     bash garage-setup.sh [--website] <bucket> <quota, e.g. 380GiB> <key name> <key file>
#
# --website also lets Garage's web endpoint serve the bucket. Each step checks first, so a rerun only sets the
# quota again. The key's secret is shown once, by Garage, when the key is made: it goes straight to <key file>
# as {"access_key_id", "secret_access_key"} (mode 600, in a directory that must exist) and is never printed.
set -euo pipefail
website=
if [ "${1:-}" = --website ]; then
	website=1
	shift
fi
usage="usage: garage-setup.sh [--website] <bucket> <quota> <key name> <key file>"
BUCKET=${1:?$usage}
QUOTA=${2:?$usage}
KEY=${3:?$usage}
KEYFILE=${4:?$usage}
g() { podman exec deploy-garage-1 /garage "$@" </dev/null; }
api() { podman exec deploy-garage-1 /garage json-api "$@" </dev/null 2>/dev/null; }
py() { python3 -c "$1" "${@:2}"; }

roles=$(api GetClusterLayout | py 'import json,sys; print(len(json.load(sys.stdin)["roles"]))')
if [ "$roles" = 0 ]; then
	node=$(api GetClusterStatus | py 'import json,sys; n=json.load(sys.stdin)["nodes"]; assert len(n) == 1, n; print(n[0]["id"])')
	# one node holds every partition whatever its capacity; the quota is what bounds the disk
	g layout assign -z earth -c "$QUOTA" "$node"
	g layout apply --version 1
fi

if ! api ListBuckets | py 'import json,sys; sys.exit(0 if any(sys.argv[1] in b["globalAliases"] for b in json.load(sys.stdin)) else 1)' "$BUCKET"; then
	g bucket create "$BUCKET"
fi
[ -z "$website" ] || g bucket website --allow "$BUCKET"
g bucket set-quotas "$BUCKET" --max-size "$QUOTA"

umask 077
if ! api ListKeys | py 'import json,sys; sys.exit(0 if any(k["name"] == sys.argv[1] for k in json.load(sys.stdin)) else 1)' "$KEY"; then
	[ ! -e "$KEYFILE" ] || { echo "$KEYFILE exists but Garage has no key $KEY" >&2; exit 1; }
	created=$(mktemp "$KEYFILE.XXXXXX")
	api CreateKey "{\"name\":\"$KEY\"}" > "$created"
	py '
import json, sys
k = json.load(open(sys.argv[1]))
with open(sys.argv[2], "w") as f:
    json.dump({"access_key_id": k["accessKeyId"], "secret_access_key": k["secretAccessKey"]}, f)
    f.write("\n")
' "$created" "$KEYFILE"
	rm "$created"
fi
[ -s "$KEYFILE" ] || { echo "Garage has key $KEY but $KEYFILE is missing" >&2; exit 1; }
g bucket allow --read --write "$BUCKET" --key "$KEY"
g bucket info "$BUCKET"
