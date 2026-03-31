#!/usr/bin/env python3
"""Generate APT repository metadata for the pantheon mirror.

Fetches Packages.gz from upstream, produces Packages.xz,
writes per-component and top-level Release files, and
optionally GPG-signs them into InRelease / Release.gpg.

Python 3.12 stdlib only (+ subprocess for gpg).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import os
import subprocess
import sys
import textwrap
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[metadata] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
REPO_ROOT = SCRIPT_DIR.parent


def load_config() -> dict:
    log(f"Loading config from {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_packages_gz(upstream: str, suite: str, component: str, arch: str) -> bytes:
    """Download Packages.gz from the upstream archive."""
    url = f"{upstream}/dists/{suite}/{component}/binary-{arch}/Packages.gz"
    log(f"Fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "apt-gh/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data: bytes = resp.read()
    log(f"  received {len(data)} bytes")
    return data


# ---------------------------------------------------------------------------
# File writing helpers
# ---------------------------------------------------------------------------

def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    log(f"  wrote {path}  ({len(data)} bytes)")


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------

def checksums(data: bytes) -> tuple[str, str, str]:
    """Return (md5, sha1, sha256) hex digests."""
    return (
        hashlib.md5(data).hexdigest(),
        hashlib.sha1(data).hexdigest(),
        hashlib.sha256(data).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Per-component Release
# ---------------------------------------------------------------------------

def write_component_release(
    dists_dir: Path,
    suite: str,
    component: str,
    arch: str,
) -> None:
    content = textwrap.dedent(f"""\
        Archive: {suite}
        Component: {component}
        Origin: apt-gh
        Label: apt-gh
        Architecture: {arch}
    """)
    rel_path = dists_dir / suite / component / f"binary-{arch}" / "Release"
    write_text(rel_path, content)


# ---------------------------------------------------------------------------
# Top-level Release
# ---------------------------------------------------------------------------

def build_top_level_release(
    dists_dir: Path,
    suite: str,
    components: list[str],
    architectures: list[str],
) -> str:
    date_str = format_datetime(datetime.now(timezone.utc))
    suite_dir = dists_dir / suite

    # Collect all files under the suite dir (Packages.gz, Packages.xz, component Release)
    file_entries: list[tuple[str, int, str, str, str]] = []  # (rel_path, size, md5, sha1, sha256)

    for component in components:
        for arch in architectures:
            comp_dir = suite_dir / component / f"binary-{arch}"
            for name in ("Packages.gz", "Packages.xz", "Release"):
                fpath = comp_dir / name
                if not fpath.exists():
                    continue
                data = fpath.read_bytes()
                md5, sha1, sha256 = checksums(data)
                rel = f"{component}/binary-{arch}/{name}"
                file_entries.append((rel, len(data), md5, sha1, sha256))

    # Build header
    lines = [
        f"Origin: apt-gh",
        f"Label: apt-gh",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {date_str}",
        f"Architectures: {' '.join(architectures)}",
        f"Components: {' '.join(components)}",
        f"Description: Ubuntu APT Mirror hosted on GitHub",
    ]

    # MD5Sum block
    lines.append("MD5Sum:")
    for rel, size, md5, _sha1, _sha256 in file_entries:
        lines.append(f" {md5} {size:>16} {rel}")

    # SHA1 block
    lines.append("SHA1:")
    for rel, size, _md5, sha1, _sha256 in file_entries:
        lines.append(f" {sha1} {size:>16} {rel}")

    # SHA256 block
    lines.append("SHA256:")
    for rel, size, _md5, _sha1, sha256 in file_entries:
        lines.append(f" {sha256} {size:>16} {rel}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GPG signing
# ---------------------------------------------------------------------------

def gpg_sign(suite_dir: Path) -> None:
    key_data = os.environ.get("GPG_PRIVATE_KEY")
    if not key_data:
        log("GPG_PRIVATE_KEY not set -- skipping signing")
        return

    log("Importing GPG private key")
    proc = subprocess.run(
        ["gpg", "--batch", "--import"],
        input=key_data.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        log(f"  gpg --import stderr: {proc.stderr.decode(errors='replace')}")
        raise RuntimeError("GPG key import failed")

    signer = "mirror@apt-definisi.pages.dev"
    release_file = suite_dir / "Release"

    # InRelease (clearsign)
    inrelease = suite_dir / "InRelease"
    log(f"Signing InRelease: {inrelease}")
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--clearsign",
            "--local-user", signer,
            "--output", str(inrelease),
            str(release_file),
        ],
        check=True,
    )

    # Release.gpg (detached armored)
    release_gpg = suite_dir / "Release.gpg"
    log(f"Signing Release.gpg: {release_gpg}")
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--detach-sign", "--armor",
            "--local-user", signer,
            "--output", str(release_gpg),
            str(release_file),
        ],
        check=True,
    )

    log("GPG signing complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    upstream: str = cfg["upstream"]
    suites: list[str] = cfg["suites"]
    components: list[str] = cfg["components"]
    architectures: list[str] = cfg["architectures"]

    dists_dir = REPO_ROOT / "dists"

    for suite in suites:
        log(f"Processing suite: {suite}")

        for component in components:
            for arch in architectures:
                log(f"  {component}/binary-{arch}")

                # 1. Fetch Packages.gz
                packages_gz = fetch_packages_gz(upstream, suite, component, arch)

                # 2. Write Packages.gz
                bin_dir = dists_dir / suite / component / f"binary-{arch}"
                write_bytes(bin_dir / "Packages.gz", packages_gz)

                # 3. Decompress and recompress as Packages.xz
                packages_raw = gzip.decompress(packages_gz)
                packages_xz = lzma.compress(packages_raw, preset=6)
                write_bytes(bin_dir / "Packages.xz", packages_xz)

                # 4. Per-component Release
                write_component_release(dists_dir, suite, component, arch)

        # 5. Top-level Release
        release_text = build_top_level_release(dists_dir, suite, components, architectures)
        release_path = dists_dir / suite / "Release"
        write_text(release_path, release_text)

        # 6. GPG sign
        gpg_sign(dists_dir / suite)

    log("Metadata generation complete")


if __name__ == "__main__":
    main()
