#!/bin/sh
# Publish the landing page, download scripts, and built releases.
#
#   python3 packaging/build-release.py --ref <commit>   # first
#   sh packaging/deploy.sh
#
# Archives are uploaded before latest.json, so latest.json never names a file
# that is not there yet. Published releases are never deleted: users may have
# pinned them with AGENTFLEET_VERSION.
set -eu

HOST=${AGENTFLEET_DEPLOY_HOST:-root@syncdedicated}
DOCROOT=${AGENTFLEET_DEPLOY_ROOT:-/var/www/agentfleet.wecansync.com}
REPO=$(cd "$(dirname "$0")/.." && pwd)

die() { printf 'deploy: %s\n' "$1" >&2; exit 1; }

[ -f "$REPO/dist/releases/latest.json" ] || die "no build found; run packaging/build-release.py first"
for file in index.html favicon.svg install.sh install.ps1; do
    [ -f "$REPO/site/$file" ] || die "missing site/$file"
done
sh -n "$REPO/site/install.sh" || die "site/install.sh has a syntax error"
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$REPO/dist/releases/latest.json")
[ -f "$REPO/dist/releases/$version/SHA256SUMS" ] || die "latest.json names $version, which was not built"
(cd "$REPO/dist/releases/$version" && sha256sum -c --quiet SHA256SUMS) || die "local archives do not match SHA256SUMS"

ssh "$HOST" "mkdir -p '$DOCROOT/releases'"
# Site files: mirror exactly, but never touch releases/.
rsync -rt --chmod=D755,F644 --delete --exclude 'releases/' "$REPO/site/" "$HOST:$DOCROOT/"
# Versioned archives, then the pointer.
rsync -rt --chmod=D755,F644 --exclude 'latest.json' "$REPO/dist/releases/" "$HOST:$DOCROOT/releases/"
rsync -t --chmod=F644 "$REPO/dist/releases/latest.json" "$HOST:$DOCROOT/releases/latest.json"
printf 'deploy: published AgentFleet %s to %s:%s\n' "$version" "$HOST" "$DOCROOT"
