#!/usr/bin/env python3
"""Regenerate manifest.json and checksums.sha256 from the working tree.

The file set is exactly what the installer's inventory preflight expects:
every file under the bundle root except .git, the repository-only
directories the installer excludes, and the two index files themselves.
Modes are normalized to 0o755 (executable) or 0o644, and the working-tree
files are chmod-ed to match so the installer's mode checks pass.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
EXCLUDED = {".ai", ".claude", ".commandcode", ".kilo", ".github", "site", "packaging", "dist"}
INDEX_FILES = {"manifest.json", "checksums.sha256"}


def main() -> int:
    files = []
    for path in sorted(ROOT.rglob("*")):
        rel_parts = path.relative_to(ROOT).parts
        if ".git" in path.parts or rel_parts[0] in EXCLUDED or path.is_dir():
            continue
        if "__pycache__" in rel_parts:
            print(f"remove generated cache first: {path}", file=sys.stderr)
            return 1
        rel = path.relative_to(ROOT).as_posix()
        if rel in INDEX_FILES:
            continue
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        mode = 0o755 if executable else 0o644
        os.chmod(path, mode)
        files.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": oct(mode).replace("0o", "0o")})
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (ROOT / "checksums.sha256").write_text("".join(f"{item['sha256']}  {item['path']}\n" for item in files), encoding="utf-8")
    os.chmod(manifest_path, 0o644)
    os.chmod(ROOT / "checksums.sha256", 0o644)
    print(f"manifest {manifest['version']}: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
