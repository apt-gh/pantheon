#!/usr/bin/env python3
"""pool_sync.py — download .deb packages and upload them as GitHub Release assets.

Runs inside each pool repo's CI via repository_dispatch or workflow_dispatch.
Python 3.12, stdlib only (+ subprocess for gh CLI).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

UPSTREAM = "http://archive.ubuntu.com/ubuntu"
PREFIX = "[pool_sync]"
MAX_RETRIES = 3
BACKOFF_SECONDS = 5


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"{PREFIX} {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{PREFIX} ERROR: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def load_packages() -> list[dict]:
    """Return the package list from env vars.

    Priority:
      1. DISPATCH_PAYLOAD (repository_dispatch — JSON string)
      2. workflow_dispatch inputs: INPUT_PACKAGES_JSON (JSON string)
    """
    raw = os.environ.get("DISPATCH_PAYLOAD", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            # repository_dispatch wraps the list under "packages"
            if isinstance(payload, dict) and "packages" in payload:
                return payload["packages"]
            if isinstance(payload, list):
                return payload
            log_error("DISPATCH_PAYLOAD is not a list or dict with 'packages' key")
            sys.exit(1)
        except json.JSONDecodeError as exc:
            log_error(f"Failed to parse DISPATCH_PAYLOAD: {exc}")
            sys.exit(1)

    # Fallback: workflow_dispatch with a packages JSON input
    raw = os.environ.get("INPUT_PACKAGES_JSON", "").strip()
    if raw:
        try:
            packages = json.loads(raw)
            if isinstance(packages, list):
                return packages
            log_error("INPUT_PACKAGES_JSON is not a list")
            sys.exit(1)
        except json.JSONDecodeError as exc:
            log_error(f"Failed to parse INPUT_PACKAGES_JSON: {exc}")
            sys.exit(1)

    log_error("No package list found. Set DISPATCH_PAYLOAD or INPUT_PACKAGES_JSON.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# GitHub helpers (via gh CLI)
# ---------------------------------------------------------------------------

def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    log(f"  gh {' '.join(args)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_release(tag: str) -> None:
    """Create the release if it doesn't already exist."""
    result = gh("release", "view", tag, "--json", "tagName", check=False)
    if result.returncode == 0:
        return
    log(f"Creating release {tag}")
    gh("release", "create", tag, "--title", tag, "--notes", f"Packages for {tag}", "--latest=false")


def existing_assets(tag: str) -> set[str]:
    """Return the set of asset filenames already uploaded to *tag*."""
    result = gh("release", "view", tag, "--json", "assets", check=False)
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
        return {a["name"] for a in data.get("assets", [])}
    except (json.JSONDecodeError, KeyError):
        return set()


def upload_asset(tag: str, filepath: Path) -> bool:
    """Upload a single file to the release. Returns True on success."""
    result = gh("release", "upload", tag, str(filepath), "--clobber", check=False)
    if result.returncode != 0:
        log_error(f"Upload failed for {filepath.name}: {result.stderr.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path, expected_size: int | None = None,
                  expected_sha256: str | None = None) -> bool:
    """Download *url* to *dest* with retries. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"  Downloading {url} (attempt {attempt}/{MAX_RETRIES})")
            urllib.request.urlretrieve(url, dest)

            # Size check
            if expected_size is not None and dest.stat().st_size != expected_size:
                log_error(f"Size mismatch for {dest.name}: "
                          f"expected {expected_size}, got {dest.stat().st_size}")
                dest.unlink(missing_ok=True)
                raise ValueError("size mismatch")

            # SHA-256 check
            if expected_sha256 is not None:
                import hashlib
                h = hashlib.sha256()
                with open(dest, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != expected_sha256:
                    log_error(f"SHA-256 mismatch for {dest.name}: "
                              f"expected {expected_sha256}, got {h.hexdigest()}")
                    dest.unlink(missing_ok=True)
                    raise ValueError("sha256 mismatch")

            return True
        except Exception as exc:
            log_error(f"Attempt {attempt} failed for {url}: {exc}")
            dest.unlink(missing_ok=True)
            if attempt < MAX_RETRIES:
                delay = BACKOFF_SECONDS * attempt
                log(f"  Retrying in {delay}s …")
                time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log_error("GITHUB_REPOSITORY is not set")
        sys.exit(1)

    if not os.environ.get("GH_TOKEN"):
        log_error("GH_TOKEN is not set")
        sys.exit(1)

    packages = load_packages()
    log(f"Received {len(packages)} package(s) to sync")

    # Group by tag: suite-component (e.g. "noble-main")
    groups: dict[str, list[dict]] = {}
    for pkg in packages:
        suite = pkg.get("suite", os.environ.get("INPUT_SUITE", "unknown"))
        component = pkg.get("component", os.environ.get("INPUT_COMPONENT", "main"))
        tag = f"{suite}-{component}"
        groups.setdefault(tag, []).append(pkg)

    download_failures = 0
    workdir = Path("_pool_sync_tmp")
    workdir.mkdir(exist_ok=True)

    for tag, tag_packages in groups.items():
        log(f"--- Processing tag: {tag} ({len(tag_packages)} packages) ---")
        ensure_release(tag)
        already = existing_assets(tag)

        for pkg in tag_packages:
            filename = pkg["filename"]
            path = pkg["path"]
            url = f"{UPSTREAM}/{path}"

            if filename in already:
                log(f"  Skipping {filename} (already uploaded)")
                continue

            dest = workdir / filename

            expected_size = pkg.get("size")
            if expected_size is not None:
                expected_size = int(expected_size)
            expected_sha = pkg.get("sha256")

            ok = download_file(url, dest, expected_size=expected_size,
                               expected_sha256=expected_sha)
            if not ok:
                download_failures += 1
                log_error(f"Failed to download {filename} after {MAX_RETRIES} attempts")
                continue

            if upload_asset(tag, dest):
                log(f"  Uploaded {filename}")
            else:
                log_error(f"Failed to upload {filename}")

            # Clean up to save disk space
            dest.unlink(missing_ok=True)

    # Clean up workdir
    try:
        workdir.rmdir()
    except OSError:
        pass

    if download_failures:
        log_error(f"{download_failures} download(s) failed")
        sys.exit(1)

    log("Sync complete")


if __name__ == "__main__":
    main()
