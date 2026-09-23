#!/usr/bin/env python3
"""Build a deterministic AgentFleet release (tar.gz + zip) from a git ref.

The bundle's own manifest.json / checksums.sha256 are read from the given git
ref (not the working tree, which may be mid-edit) via `git show REF:path`.
Every manifest entry is fetched the same way and verified against its
checksum before any archive bytes are written. Two invocations against the
same ref must produce byte-identical archives: all timestamps are derived
from the ref's commit time, not wall-clock time, and ownership/name fields
are fixed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def fail(message: str) -> None:
    print(f"build-release: {message}", file=sys.stderr)
    raise SystemExit(1)


def git_show_bytes(ref: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        fail(f"failed to read {path} at {ref}: {stderr}")
    return proc.stdout


def git_commit_ts(ref: str) -> int:
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%ct", ref],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        fail(f"failed to resolve commit time for {ref}: {stderr}")
    return int(proc.stdout.decode("ascii").strip())


def git_commit_sha(ref: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        fail(f"failed to resolve commit sha for {ref}: {stderr}")
    return proc.stdout.decode("ascii").strip()


def mode_to_int(mode: str) -> int:
    if mode == "0o755":
        return 0o755
    if mode == "0o644":
        return 0o644
    fail(f"invalid mode in manifest: {mode!r}")
    raise AssertionError("unreachable")


def load_release_inputs(ref: str) -> tuple[dict, dict[str, bytes], str]:
    """Read manifest, checksums, VERSION and every listed file from the ref.

    Returns (manifest_dict, {path: bytes} including manifest.json and
    checksums.sha256, version_string).
    """
    manifest_bytes = git_show_bytes(ref, "manifest.json")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"manifest.json at {ref} is not valid JSON: {exc}")
        raise AssertionError("unreachable")

    checksums_bytes = git_show_bytes(ref, "checksums.sha256")
    checksums_text = checksums_bytes.decode("utf-8")
    listed: dict[str, str] = {}
    for line_number, line in enumerate(checksums_text.splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            fail(f"invalid checksums.sha256 line {line_number} at {ref}")
        digest, rel = match.groups()
        if rel in listed:
            fail(f"checksums.sha256 lists {rel} more than once")
        listed[rel] = digest

    version_bytes = git_show_bytes(ref, "VERSION")
    version = version_bytes.decode("utf-8").strip()

    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        fail("manifest.files is empty or invalid")

    expected: dict[str, dict] = {}
    for item in records:
        if not isinstance(item, dict):
            fail("manifest contains a non-object file record")
        rel = item.get("path")
        digest = item.get("sha256")
        mode = item.get("mode")
        if not isinstance(rel, str) or not rel:
            fail(f"manifest has an invalid path: {rel!r}")
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or ":" in parts[0] or any(part in {"", ".", ".."} for part in parts):
            fail(f"manifest path is not a safe relative path: {rel!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail(f"manifest has an invalid checksum for {rel}")
        if mode not in {"0o644", "0o755"}:
            fail(f"manifest mode for {rel} must be logical 0o644 or 0o755")
        if rel in expected:
            fail(f"manifest lists {rel} more than once")
        expected[rel] = item

    if set(listed) != set(expected):
        extras = sorted(set(listed) - set(expected))
        missing = sorted(set(expected) - set(listed))
        details = []
        if extras:
            details.append("checksums has unlisted: " + ", ".join(extras))
        if missing:
            details.append("checksums missing: " + ", ".join(missing))
        fail("checksums.sha256 does not have exact parity with manifest.json (" + "; ".join(details) + ")")
    for rel, digest in listed.items():
        if digest != expected[rel]["sha256"]:
            fail(f"checksums.sha256 disagrees with manifest.json for {rel}")

    contents: dict[str, bytes] = {
        "manifest.json": manifest_bytes,
        "checksums.sha256": checksums_bytes,
    }
    for rel, item in expected.items():
        data = git_show_bytes(ref, rel)
        actual = hashlib.sha256(data).hexdigest()
        if actual != item["sha256"]:
            fail(f"checksum mismatch for {rel} at {ref}: expected {item['sha256']}, got {actual}")
        contents[rel] = data

    return manifest, contents, version


def collect_dir_paths(file_paths: list[str]) -> list[str]:
    dirs: set[str] = set()
    for rel in file_paths:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return sorted(dirs)


def build_tar_gz(
    out_path: Path,
    top_dir: str,
    contents: dict[str, bytes],
    file_modes: dict[str, int],
    mtime: int,
) -> None:
    file_paths = sorted(contents)
    dir_paths = collect_dir_paths(file_paths)
    # Include the top-level directory itself.
    all_dirs = sorted(set(dir_paths) | {""})

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for d in all_dirs:
            arcname = f"{top_dir}/{d}" if d else top_dir
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = mtime
            info.size = 0
            tf.addfile(info)
        for rel in file_paths:
            data = contents[rel]
            arcname = f"{top_dir}/{rel}"
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.REGTYPE
            info.mode = file_modes[rel]
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = mtime
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    tar_bytes = buf.getvalue()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(tar_bytes)


def build_zip(
    out_path: Path,
    top_dir: str,
    contents: dict[str, bytes],
    file_modes: dict[str, int],
    mtime: int,
) -> None:
    file_paths = sorted(contents)
    dir_paths = collect_dir_paths(file_paths)

    date_time = dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc)
    dt_tuple = (date_time.year, date_time.month, date_time.day, date_time.hour, date_time.minute, date_time.second)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in dir_paths:
            arcname = f"{top_dir}/{d}/"
            info = zipfile.ZipInfo(filename=arcname, date_time=dt_tuple)
            info.create_system = 3  # unix
            info.external_attr = (0o755 << 16) | 0x10  # dir mode + MS-DOS dir flag
            zf.writestr(info, b"")
        for rel in file_paths:
            data = contents[rel]
            arcname = f"{top_dir}/{rel}"
            info = zipfile.ZipInfo(filename=arcname, date_time=dt_tuple)
            info.create_system = 3  # unix
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (file_modes[rel] << 16)
            zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic AgentFleet release from a git ref")
    parser.add_argument("--ref", default="HEAD", help="git ref to build from (default: HEAD)")
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument("--version", default=None, help="expected version; must match VERSION at --ref")
    args = parser.parse_args(argv)

    manifest, contents, version_at_ref = load_release_inputs(args.ref)
    manifest_version = manifest.get("version")
    if manifest_version != version_at_ref:
        fail(
            f"VERSION at {args.ref} ({version_at_ref!r}) does not match "
            f"manifest.json version ({manifest_version!r})"
        )
    if not VERSION_RE.fullmatch(version_at_ref):
        fail(f"VERSION at {args.ref} is not a valid semver: {version_at_ref!r}")

    if args.version is not None and args.version != version_at_ref:
        fail(
            f"--version {args.version!r} does not match VERSION at {args.ref} "
            f"({version_at_ref!r})"
        )
    version = version_at_ref

    file_modes: dict[str, int] = {"manifest.json": 0o644, "checksums.sha256": 0o644}
    for item in manifest["files"]:
        file_modes[item["path"]] = mode_to_int(item["mode"])

    commit_ts = git_commit_ts(args.ref)
    commit_sha = git_commit_sha(args.ref)

    out_dir = Path(args.out) / "releases" / version
    top_dir = f"agentfleet-{version}"

    tar_path = out_dir / f"agentfleet-{version}.tar.gz"
    zip_path = out_dir / f"agentfleet-{version}.zip"

    build_tar_gz(tar_path, top_dir, contents, file_modes, commit_ts)
    build_zip(zip_path, top_dir, contents, file_modes, commit_ts)

    tar_sha = sha256_of_file(tar_path)
    zip_sha = sha256_of_file(zip_path)

    sums_path = out_dir / "SHA256SUMS"
    sums_path.write_text(
        f"{tar_sha}  {tar_path.name}\n{zip_sha}  {zip_path.name}\n",
        encoding="utf-8",
    )

    published_at = (
        dt.datetime.fromtimestamp(commit_ts, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    latest = {
        "format": "agentfleet.release.v1",
        "version": version,
        "commit": commit_sha,
        "published_at": published_at,
        "tarball": f"releases/{version}/agentfleet-{version}.tar.gz",
        "tarball_sha256": tar_sha,
        "zip": f"releases/{version}/agentfleet-{version}.zip",
        "zip_sha256": zip_sha,
    }
    latest_path = Path(args.out) / "releases" / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    print(f"build-release: version={version} commit={commit_sha}")
    print(f"build-release: tarball={tar_path} sha256={tar_sha}")
    print(f"build-release: zip={zip_path} sha256={zip_sha}")
    print(f"build-release: latest.json={latest_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
