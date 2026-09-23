#!/bin/sh
# AgentFleet bootstrap installer.
#
# Usage:
#   curl -fsSL https://agentfleet.wecansync.com/install.sh | sh
#   irm https://agentfleet.wecansync.com/install.ps1 | iex   (Windows, see install.ps1)
#
# This script is intentionally POSIX sh: it must run correctly under dash,
# busybox sh, and bash without modification. Everything is inside functions
# and the only top-level statement is the final `main "$@"` call, so a
# truncated download (e.g. a network cut mid-stream when piped into `sh`)
# cannot execute a partial, unintended command.
set -eu

AGENTFLEET_DEFAULT_BASE_URL="https://agentfleet.wecansync.com"

log() {
    printf 'agentfleet: %s\n' "$1"
}

err() {
    printf 'agentfleet: %s\n' "$1" >&2
}

fail() {
    err "$1"
    exit 1
}

cleanup() {
    if [ -n "${AGENTFLEET_TMPDIR:-}" ] && [ -d "${AGENTFLEET_TMPDIR:-}" ]; then
        rm -rf "$AGENTFLEET_TMPDIR"
    fi
}

# Extract the hostname (without scheme, path, or port) from a URL of the
# form scheme://host[:port][/path...]. POSIX sh has no regex capture, so this
# is done with parameter expansion only.
url_host() {
    _uh_rest=$1
    _uh_rest=${_uh_rest#*://}
    _uh_rest=${_uh_rest%%/*}
    # Strip a trailing :port, but keep IPv6 literals ([::1]) intact.
    case $_uh_rest in
        \[*\]*)
            _uh_rest=${_uh_rest%%]*}]
            ;;
        *)
            _uh_rest=${_uh_rest%%:*}
            ;;
    esac
    printf '%s' "$_uh_rest"
}

validate_base_url() {
    _bu_url=$1
    case $_bu_url in
        https://*)
            return 0
            ;;
        http://*)
            _bu_host=$(url_host "$_bu_url")
            case $_bu_host in
                127.0.0.1 | localhost | localhost:* | \[::1\])
                    return 0
                    ;;
                *)
                    fail "AGENTFLEET_BASE_URL must use https:// (http:// is only allowed for 127.0.0.1/localhost): $_bu_url"
                    ;;
            esac
            ;;
        *)
            fail "AGENTFLEET_BASE_URL must be an http(s) URL: $_bu_url"
            ;;
    esac
}

resolve_base_url() {
    _rbu_url=${AGENTFLEET_BASE_URL:-$AGENTFLEET_DEFAULT_BASE_URL}
    validate_base_url "$_rbu_url"
    printf '%s' "$_rbu_url"
}

detect_downloader() {
    if command -v curl >/dev/null 2>&1; then
        printf 'curl'
    elif command -v wget >/dev/null 2>&1; then
        printf 'wget'
    else
        fail "neither curl nor wget is available; install one and retry"
    fi
}

# Fetch a URL to stdout. $1 = downloader ("curl" or "wget"), $2 = URL.
fetch_url() {
    _fu_downloader=$1
    _fu_url=$2
    case $_fu_url in
        https://*)
            if [ "$_fu_downloader" = "curl" ]; then
                curl -fsSL --proto '=https' --tlsv1.2 "$_fu_url"
            else
                wget -qO- "$_fu_url"
            fi
            ;;
        *)
            if [ "$_fu_downloader" = "curl" ]; then
                curl -fsSL "$_fu_url"
            else
                wget -qO- "$_fu_url"
            fi
            ;;
    esac
}

# Fetch a URL to a file. $1 = downloader, $2 = URL, $3 = destination path.
fetch_url_to_file() {
    _fuf_downloader=$1
    _fuf_url=$2
    _fuf_dest=$3
    case $_fuf_url in
        https://*)
            if [ "$_fuf_downloader" = "curl" ]; then
                curl -fsSL --proto '=https' --tlsv1.2 -o "$_fuf_dest" "$_fuf_url"
            else
                wget -qO "$_fuf_dest" "$_fuf_url"
            fi
            ;;
        *)
            if [ "$_fuf_downloader" = "curl" ]; then
                curl -fsSL -o "$_fuf_dest" "$_fuf_url"
            else
                wget -qO "$_fuf_dest" "$_fuf_url"
            fi
            ;;
    esac
}

check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 (>= 3.10) is required but was not found on PATH."
        err "  macOS (Homebrew): brew install python@3.12"
        err "  Debian/Ubuntu:    sudo apt install python3"
        err "  Other:            https://www.python.org/downloads/"
        exit 1
    fi
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        err "python3 >= 3.10 is required (found an older version)."
        err "  macOS (Homebrew): brew install python@3.12"
        err "  Debian/Ubuntu:    sudo apt install python3"
        err "  Other:            https://www.python.org/downloads/"
        exit 1
    fi
}

check_node() {
    if ! command -v node >/dev/null 2>&1; then
        err "Node.js (>= 18) is required but was not found on PATH."
        err "  macOS (Homebrew): brew install node"
        err "  Debian/Ubuntu:    sudo apt install nodejs"
        err "  Other:            https://nodejs.org/"
        exit 1
    fi
    _cn_major=$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))' 2>/dev/null || echo 0)
    case $_cn_major in
        ''|*[!0-9]*)
            _cn_major=0
            ;;
    esac
    if [ "$_cn_major" -lt 18 ]; then
        err "Node.js >= 18 is required (found an older version)."
        err "  macOS (Homebrew): brew install node"
        err "  Debian/Ubuntu:    sudo apt install nodejs"
        err "  Other:            https://nodejs.org/"
        exit 1
    fi
}

check_claude() {
    if ! command -v claude >/dev/null 2>&1; then
        log "warning: 'claude' (Claude Code) was not found on PATH."
        log "  install it with: curl -fsSL https://claude.ai/install.sh | bash"
    fi
}

check_root() {
    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
        log "warning: running as root. AgentFleet installs into a user home directory and does not require root."
    fi
}

validate_version_string() {
    _vvs_v=$1
    case $_vvs_v in
        *[!0-9.]*)
            fail "invalid version string: $_vvs_v"
            ;;
    esac
    case $_vvs_v in
        [0-9]*.[0-9]*.[0-9]*) ;;
        *)
            fail "invalid version string: $_vvs_v"
            ;;
    esac
    # Reject extra dot-separated segments or empty segments (e.g. 1.2.3.4,
    # 1..2, ../../x after the digit filter above would already have failed,
    # but be defensive about segment count too).
    _vvs_segs=$(printf '%s' "$_vvs_v" | awk -F. '{print NF}')
    if [ "$_vvs_segs" != "3" ]; then
        fail "invalid version string: $_vvs_v"
    fi
}

# Extract the sha256 for a given filename out of a SHA256SUMS-style listing
# read from stdin: lines of the form "<hex>  <filename>".
sha256_from_sums() {
    _fsf_name=$1
    awk -v name="$_fsf_name" '$2 == name { print $1; found=1 } END { if (!found) exit 1 }'
}

json_get_str() {
    # $1 = json text, $2 = key. Uses python3's json module (no jq dependency).
    printf '%s' "$1" | python3 -c '
import json, sys
data = json.load(sys.stdin)
key = sys.argv[1]
value = data.get(key)
if value is None:
    sys.exit(1)
sys.stdout.write(str(value))
' "$2"
}

sha256_file() {
    _sf_path=$1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$_sf_path" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$_sf_path" | awk '{print $1}'
    else
        python3 -c '
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1048576), b""):
        h.update(chunk)
print(h.hexdigest())
' "$_sf_path"
    fi
}

# Resolve which version + tarball sha256 to install. Sets globals:
# RESOLVED_VERSION, RESOLVED_TARBALL_SHA256, RESOLVED_TARBALL_URL.
resolve_release() {
    _rr_downloader=$1
    _rr_base_url=$2

    if [ -n "${AGENTFLEET_VERSION:-}" ]; then
        validate_version_string "$AGENTFLEET_VERSION"
        RESOLVED_VERSION=$AGENTFLEET_VERSION
        _rr_sums_url="$_rr_base_url/releases/$RESOLVED_VERSION/SHA256SUMS"
        _rr_sums=$(fetch_url "$_rr_downloader" "$_rr_sums_url") || fail "failed to fetch $_rr_sums_url"
        _rr_tar_name="agentfleet-$RESOLVED_VERSION.tar.gz"
        RESOLVED_TARBALL_SHA256=$(printf '%s\n' "$_rr_sums" | sha256_from_sums "$_rr_tar_name") \
            || fail "SHA256SUMS at $_rr_sums_url has no entry for $_rr_tar_name"
        RESOLVED_TARBALL_URL="$_rr_base_url/releases/$RESOLVED_VERSION/$_rr_tar_name"
    else
        _rr_latest_url="$_rr_base_url/releases/latest.json"
        _rr_latest_json=$(fetch_url "$_rr_downloader" "$_rr_latest_url") || fail "failed to fetch $_rr_latest_url"
        RESOLVED_VERSION=$(json_get_str "$_rr_latest_json" version) \
            || fail "latest.json at $_rr_latest_url has no 'version' field"
        validate_version_string "$RESOLVED_VERSION"
        _rr_tarball_rel=$(json_get_str "$_rr_latest_json" tarball) \
            || fail "latest.json at $_rr_latest_url has no 'tarball' field"
        _rr_expected_rel="releases/$RESOLVED_VERSION/agentfleet-$RESOLVED_VERSION.tar.gz"
        if [ "$_rr_tarball_rel" != "$_rr_expected_rel" ]; then
            fail "latest.json 'tarball' field ($_rr_tarball_rel) does not match expected path ($_rr_expected_rel)"
        fi
        RESOLVED_TARBALL_SHA256=$(json_get_str "$_rr_latest_json" tarball_sha256) \
            || fail "latest.json at $_rr_latest_url has no 'tarball_sha256' field"
        RESOLVED_TARBALL_URL="$_rr_base_url/$_rr_tarball_rel"
    fi
}

agentfleet_home() {
    if [ -n "${AGENTFLEET_HOME:-}" ]; then
        printf '%s' "$AGENTFLEET_HOME"
    else
        printf '%s/agentfleet' "${XDG_DATA_HOME:-$HOME/.local/share}"
    fi
}

# Download, verify, extract, and install the release tarball. Sets global
# RELEASE_DIR to the installed release directory on success.
download_and_install() {
    _dai_downloader=$1
    _dai_tarball_url=$2
    _dai_expected_sha256=$3
    _dai_version=$4
    _dai_home=$5

    AGENTFLEET_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/agentfleet.XXXXXX")
    _dai_tar_path="$AGENTFLEET_TMPDIR/agentfleet-$_dai_version.tar.gz"

    log "downloading agentfleet $_dai_version..."
    fetch_url_to_file "$_dai_downloader" "$_dai_tarball_url" "$_dai_tar_path" \
        || fail "failed to download $_dai_tarball_url"

    _dai_actual_sha256=$(sha256_file "$_dai_tar_path")
    if [ "$_dai_actual_sha256" != "$_dai_expected_sha256" ]; then
        err "checksum verification failed for $_dai_tar_path"
        err "  expected: $_dai_expected_sha256"
        err "  actual:   $_dai_actual_sha256"
        exit 1
    fi
    log "checksum verified."

    _dai_stage_root="$_dai_home/.staging"
    mkdir -p "$_dai_stage_root"
    _dai_stage_dir=$(mktemp -d "$_dai_stage_root/build.XXXXXX")

    # Defense in depth: every entry must sit under agentfleet-<version>/ with
    # no absolute path or ".." component, whatever tar implementation runs.
    _dai_listing=$(tar -tzf "$_dai_tar_path" 2>/dev/null) || fail "failed to read $_dai_tar_path"
    printf '%s\n' "$_dai_listing" | awk -v top="agentfleet-$_dai_version/" '
        index($0, top) != 1 { bad = 1 }
        /^\// || /(^|\/)\.\.(\/|$)/ { bad = 1 }
        END { exit bad ? 1 : 0 }' || fail "release archive contains unexpected paths; refusing to extract"

    if ! tar -xzpf "$_dai_tar_path" -C "$_dai_stage_dir" 2>/dev/null; then
        fail "failed to extract $_dai_tar_path"
    fi

    _dai_extracted="$_dai_stage_dir/agentfleet-$_dai_version"
    if [ ! -f "$_dai_extracted/bin/install.py" ]; then
        fail "extracted release is missing bin/install.py; refusing to install"
    fi

    mkdir -p "$_dai_home/releases"
    _dai_target="$_dai_home/releases/$_dai_version"
    if [ -e "$_dai_target" ]; then
        rm -rf "$_dai_target"
    fi
    mv "$_dai_extracted" "$_dai_target"
    rm -rf "$_dai_stage_dir"

    printf '%s\n' "$_dai_version" >"$_dai_home/current"

    RELEASE_DIR=$_dai_target
}

have_usable_tty() {
    (exec 3</dev/tty) 2>/dev/null
}

run_bundle_installer() {
    _rbi_release_dir=$1
    shift
    _rbi_installer="$_rbi_release_dir/bin/install.py"

    if [ -t 0 ]; then
        python3 "$_rbi_installer" "$@"
        return $?
    fi

    if [ -n "${AGENTFLEET_NONINTERACTIVE:-}" ]; then
        python3 "$_rbi_installer" "$@"
        return $?
    fi

    if have_usable_tty; then
        python3 "$_rbi_installer" "$@" </dev/tty
        return $?
    fi

    python3 "$_rbi_installer" "$@"
    return $?
}

main() {
    trap cleanup EXIT
    # An interrupt must stop the script, not just clean up and carry on.
    trap 'cleanup; exit 130' INT
    trap 'cleanup; exit 143' TERM

    check_root
    check_python
    check_node
    check_claude

    _m_base_url=$(resolve_base_url)
    _m_downloader=$(detect_downloader)

    resolve_release "$_m_downloader" "$_m_base_url"
    log "resolved version: $RESOLVED_VERSION"

    _m_home=$(agentfleet_home)
    mkdir -p "$_m_home"

    download_and_install "$_m_downloader" "$RESOLVED_TARBALL_URL" "$RESOLVED_TARBALL_SHA256" "$RESOLVED_VERSION" "$_m_home"
    log "installed to $RELEASE_DIR"

    log "running installer..."
    # Under set -e a failing installer ends the script with its own status.
    run_bundle_installer "$RELEASE_DIR" "$@"
}

main "$@"
