#!/usr/bin/env python3
"""Pantheon orchestrator — fetches APT Packages indexes, maps packages to pool repos,
and dispatches repository_dispatch events to trigger per-repo syncs."""

import argparse
import gzip
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
REPO_MAP_PATH = SCRIPT_DIR / "repo_map.json"
TMP_DIR = SCRIPT_DIR.parent / "tmp"

DISPATCH_PAYLOAD_LIMIT = 60000
BATCH_SIZE = 500


def log(msg: str) -> None:
    print(f"[orchestrator] {msg}", flush=True)


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# APT Packages parsing
# ---------------------------------------------------------------------------

def packages_url(upstream: str, suite: str, component: str, arch: str) -> str:
    return f"{upstream}/dists/{suite}/{component}/binary-{arch}/Packages.gz"


def parse_packages(raw: str) -> list[dict]:
    """Parse APT Packages format into a list of dicts.

    Key-value pairs separated by blank lines.  Continuation lines start with
    a space or tab and are appended to the previous key's value.
    """
    entries: list[dict] = []
    current: dict[str, str] = {}
    last_key: str | None = None

    for line in raw.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
                last_key = None
            continue

        if line[0] in (" ", "\t"):
            # continuation line
            if last_key is not None:
                current[last_key] += "\n" + line
            continue

        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            current[key] = value
            last_key = key

    # last stanza if file doesn't end with blank line
    if current:
        entries.append(current)

    return entries


def fetch_packages(upstream: str, suite: str, component: str, arch: str) -> list[dict]:
    url = packages_url(upstream, suite, component, arch)
    log(f"fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pantheon-orchestrator/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        compressed = resp.read()
    raw = gzip.decompress(compressed).decode("utf-8", errors="replace")
    entries = parse_packages(raw)
    log(f"  parsed {len(entries)} packages for {suite}/{component}/{arch}")
    return entries


# ---------------------------------------------------------------------------
# Package-to-repo mapping
# ---------------------------------------------------------------------------

def map_package_to_repo(filename_path: str, pool: dict, pool_lib: dict) -> str:
    """Determine the target pool repo from the Filename: field in Packages.

    The Filename field looks like: pool/main/a/apr/libapr1t64_1.7.2_amd64.deb
    The prefix directory (3rd component) determines the repo:
      - 'a'    → pool['a']    → apollo
      - 'liba' → pool_lib['liba'] → atlas
    """
    parts = filename_path.split("/")
    if len(parts) < 4:
        return pool.get("0", "omega")
    prefix = parts[2]  # e.g., 'a', 'liba', 'h', 'libx'

    # Check lib* prefix first
    if prefix.startswith("lib") and len(prefix) >= 4:
        fourth = prefix[3]
        key = f"lib{fourth}" if not fourth.isdigit() else "lib0"
        repo = pool_lib.get(key)
        if repo:
            return repo

    # Regular prefix — use first character
    first = prefix[0] if prefix else "0"
    key = first if not first.isdigit() else "0"
    return pool.get(key, pool.get("0", "omega"))


# ---------------------------------------------------------------------------
# Fetch mode
# ---------------------------------------------------------------------------

def do_fetch(config: dict, repo_map: dict) -> None:
    pool = repo_map["pool"]
    pool_lib = repo_map["pool_lib"]
    upstream = config["upstream"]

    manifests: dict[str, list[dict]] = {}

    for suite in config["suites"]:
        for component in config["components"]:
            for arch in config["architectures"]:
                entries = fetch_packages(upstream, suite, component, arch)
                for entry in entries:
                    pkg_name = entry.get("Package", "")
                    filename_field = entry.get("Filename", "")
                    if not pkg_name or not filename_field:
                        continue

                    repo_name = map_package_to_repo(filename_field, pool, pool_lib)
                    filename = filename_field.rsplit("/", 1)[-1]

                    record = {
                        "package": pkg_name,
                        "filename": filename,
                        "path": filename_field,
                        "suite": suite,
                        "component": component,
                        "arch": arch,
                        "size": entry.get("Size", ""),
                        "sha256": entry.get("SHA256", ""),
                    }

                    manifests.setdefault(repo_name, []).append(record)

    # Write manifests to tmp/
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    for repo_name, records in manifests.items():
        out_path = TMP_DIR / f"{repo_name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, separators=(",", ":"))
        log(f"wrote {len(records)} entries to {out_path.name}")

    log(f"fetch complete — {sum(len(r) for r in manifests.values())} packages across {len(manifests)} repos")


# ---------------------------------------------------------------------------
# Dispatch mode
# ---------------------------------------------------------------------------

def dispatch_repo(org: str, repo_name: str, packages: list[dict]) -> bool:
    """Send repository_dispatch event(s) for a single pool repo.

    If the payload exceeds the GitHub size limit, batch into groups of
    BATCH_SIZE packages.
    """
    batches: list[list[dict]] = []

    # Check if single payload fits
    payload_str = json.dumps({"packages": packages}, separators=(",", ":"))
    if len(payload_str) <= DISPATCH_PAYLOAD_LIMIT:
        batches.append(packages)
    else:
        for i in range(0, len(packages), BATCH_SIZE):
            batches.append(packages[i : i + BATCH_SIZE])

    ok = True
    for idx, batch in enumerate(batches):
        client_payload = json.dumps({"packages": batch}, separators=(",", ":"))

        # Double-check batch size; sub-split if still too large
        if len(client_payload) > DISPATCH_PAYLOAD_LIMIT:
            half = len(batch) // 2
            sub_ok_a = dispatch_repo(org, repo_name, batch[:half])
            sub_ok_b = dispatch_repo(org, repo_name, batch[half:])
            if not sub_ok_a or not sub_ok_b:
                ok = False
            continue

        batch_label = f" (batch {idx + 1}/{len(batches)})" if len(batches) > 1 else ""
        log(f"dispatching {repo_name}{batch_label} — {len(batch)} packages, {len(client_payload)} chars")

        try:
            # Build full request body as JSON — client_payload must be an object, not a string
            request_body = json.dumps({
                "event_type": "sync",
                "client_payload": json.loads(client_payload),
            })
            result = subprocess.run(
                [
                    "gh", "api",
                    f"repos/{org}/{repo_name}/dispatches",
                    "--method", "POST",
                    "--input", "-",
                ],
                input=request_body,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log(f"  ERROR dispatching {repo_name}: {result.stderr.strip()}")
                ok = False
            else:
                log(f"  ok")
        except subprocess.TimeoutExpired:
            log(f"  ERROR dispatching {repo_name}: timeout")
            ok = False
        except FileNotFoundError:
            log("  ERROR: gh CLI not found — install GitHub CLI")
            ok = False

    return ok


def do_dispatch(config: dict) -> bool:
    org = config["org"]
    if not TMP_DIR.is_dir():
        log("ERROR: tmp/ directory not found — run --fetch first")
        return False

    all_ok = True
    manifest_files = sorted(TMP_DIR.glob("*.json"))
    if not manifest_files:
        log("no manifests found in tmp/ — nothing to dispatch")
        return True

    log(f"dispatching to {len(manifest_files)} repos")
    for mf in manifest_files:
        repo_name = mf.stem
        with open(mf, "r", encoding="utf-8") as f:
            packages = json.load(f)
        if not packages:
            continue
        if not dispatch_repo(org, repo_name, packages):
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Pantheon orchestrator")
    parser.add_argument("--fetch", action="store_true", help="Fetch and parse Packages.gz, write manifests")
    parser.add_argument("--dispatch", action="store_true", help="Read manifests and dispatch to pool repos")
    args = parser.parse_args()

    run_fetch = args.fetch
    run_dispatch = args.dispatch

    # No flags = both steps
    if not run_fetch and not run_dispatch:
        run_fetch = True
        run_dispatch = True

    config = load_json(CONFIG_PATH)
    repo_map = load_json(REPO_MAP_PATH)

    if run_fetch:
        do_fetch(config, repo_map)

    if run_dispatch:
        ok = do_dispatch(config)
        if not ok:
            log("one or more dispatches failed")
            sys.exit(1)

    log("done")


if __name__ == "__main__":
    main()
