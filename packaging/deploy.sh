#!/bin/sh
# Publish the landing page, download scripts, and built releases.
#
#   python3 packaging/build-release.py --ref <commit>   # first
#   AGENTFLEET_DEPLOY_HOST=user@server sh packaging/deploy.sh --dry-run
#   AGENTFLEET_DEPLOY_HOST=user@server sh packaging/deploy.sh
#
# Archives are uploaded before latest.json, so latest.json never names a file
# that is not there yet. Published releases are never deleted: users may have
# pinned them with AGENTFLEET_VERSION.
#
# The site sync deletes remote files that are not in site/, so it only runs
# against a docroot that is empty or already carries the .agentfleet-docroot
# marker written by an earlier deploy. --dry-run lists every change and
# deletion without writing anything.
set -eu

die() { printf 'deploy: %s\n' "$1" >&2; exit 1; }

DRY_RUN=
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    *) die "usage: sh packaging/deploy.sh [--dry-run]" ;;
esac
HOST=${AGENTFLEET_DEPLOY_HOST:-}
[ -n "$HOST" ] || die "set AGENTFLEET_DEPLOY_HOST to the ssh destination (user@server)"
DOCROOT=${AGENTFLEET_DEPLOY_ROOT:-/var/www/agentfleet.wecansync.com}
case "$DOCROOT" in
    /*/*) ;;
    *) die "AGENTFLEET_DEPLOY_ROOT must be an absolute path at least two levels deep" ;;
esac
case "$DOCROOT" in
    *[!A-Za-z0-9._/-]* | */../* | */.. | */./* | */.) die "AGENTFLEET_DEPLOY_ROOT has unsafe characters or segments" ;;
esac
DOCROOT=${DOCROOT%/}
MARKER=.agentfleet-docroot
REPO=$(cd "$(dirname "$0")/.." && pwd)

[ -f "$REPO/dist/releases/latest.json" ] || die "no build found; run packaging/build-release.py first"
for file in index.html favicon.svg install.sh install.ps1; do
    [ -f "$REPO/site/$file" ] || die "missing site/$file"
done
sh -n "$REPO/site/install.sh" || die "site/install.sh has a syntax error"
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$REPO/dist/releases/latest.json")
[ -f "$REPO/dist/releases/$version/SHA256SUMS" ] || die "latest.json names $version, which was not built"
(cd "$REPO/dist/releases/$version" && sha256sum -c --quiet SHA256SUMS) || die "local archives do not match SHA256SUMS"

# Refuse a docroot that holds anything this script did not put there.
state=$(ssh "$HOST" "if [ -f '$DOCROOT/$MARKER' ]; then echo claimed; elif [ ! -e '$DOCROOT' ] || [ -z \"\$(ls -A '$DOCROOT')\" ]; then echo empty; else echo foreign; fi")
case "$state" in
    claimed | empty) ;;
    foreign) die "$HOST:$DOCROOT is not empty and has no $MARKER marker; refusing to sync with --delete" ;;
    *) die "could not inspect $HOST:$DOCROOT" ;;
esac

if [ -n "$DRY_RUN" ]; then
    printf 'deploy: dry run against %s:%s (%s)\n' "$HOST" "$DOCROOT" "$state"
    rsync -rtn -i --chmod=D755,F644 --delete --exclude 'releases/' --exclude "$MARKER" "$REPO/site/" "$HOST:$DOCROOT/" || [ "$state" = empty ]
    rsync -rtn -i --chmod=D755,F644 "$REPO/dist/releases/" "$HOST:$DOCROOT/releases/" || [ "$state" = empty ]
    exit 0
fi

ssh "$HOST" "mkdir -p '$DOCROOT/releases' && touch '$DOCROOT/$MARKER'"
# Site files: mirror exactly, but never touch releases/ or the marker.
rsync -rt --chmod=D755,F644 --delete --exclude 'releases/' --exclude "$MARKER" "$REPO/site/" "$HOST:$DOCROOT/"
# Versioned archives, then the pointer.
rsync -rt --chmod=D755,F644 --exclude 'latest.json' "$REPO/dist/releases/" "$HOST:$DOCROOT/releases/"
rsync -t --chmod=F644 "$REPO/dist/releases/latest.json" "$HOST:$DOCROOT/releases/latest.json"
printf 'deploy: published AgentFleet %s to %s:%s\n' "$version" "$HOST" "$DOCROOT"
