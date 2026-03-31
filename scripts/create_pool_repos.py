#!/usr/bin/env python3
"""create_pool_repos.py — create all pool repos in the apt-gh GitHub org.

Reads scripts/repo_map.json for the full list of pool repo names, then for each:
  1. Checks if the repo already exists (skips if so)
  2. Creates the repo via `gh repo create`
  3. Initialises it with README, .gitignore, workflow, and pool_sync.py
  4. Pushes to the new remote

Usage:
    python scripts/create_pool_repos.py                # create all repos
    python scripts/create_pool_repos.py --repo phoenix  # create one repo
    python scripts/create_pool_repos.py --dry-run       # preview only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ORG = "apt-gh"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_MAP_PATH = SCRIPT_DIR / "repo_map.json"
POOL_SYNC_PATH = SCRIPT_DIR / "pool_sync.py"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SYNC_WORKFLOW = """\
name: Sync Pool Packages

on:
  repository_dispatch:
    types: [sync]
  workflow_dispatch:
    inputs:
      suite:
        description: "Suite to sync (e.g., noble)"
        required: false
      component:
        description: "Component to sync (e.g., main)"
        required: false

permissions:
  contents: write

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 360
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Sync packages
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          DISPATCH_PAYLOAD: ${{ toJson(github.event.client_payload) }}
          INPUT_SUITE: ${{ github.event.inputs.suite }}
          INPUT_COMPONENT: ${{ github.event.inputs.component }}
        run: python scripts/pool_sync.py
"""

README_TEMPLATE = """\
# {repo_name}

{description}

This repository is part of the [apt-gh](https://github.com/apt-gh) Ubuntu APT mirror.
Packages are stored as GitHub Release assets, organized by suite and component.
"""

GITIGNORE = """\
__pycache__/
*.pyc
tmp/
*.log
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check, **kwargs)


def repo_exists(name: str) -> bool:
    """Return True if apt-gh/{name} already exists on GitHub."""
    result = run(["gh", "repo", "view", f"{ORG}/{name}"], check=False)
    return result.returncode == 0


def build_repo_list() -> list[tuple[str, str]]:
    """Return [(repo_name, description), ...] from repo_map.json."""
    with open(REPO_MAP_PATH) as f:
        repo_map = json.load(f)

    repos: list[tuple[str, str]] = []

    for prefix_letter, repo_name in sorted(repo_map.get("pool", {}).items()):
        desc = f"APT pool packages for prefix '{prefix_letter}'"
        repos.append((repo_name, desc))

    for prefix, repo_name in sorted(repo_map.get("pool_lib", {}).items()):
        desc = f"APT pool packages for lib prefix '{prefix}'"
        repos.append((repo_name, desc))

    return repos


def create_repo(name: str, description: str, dry_run: bool) -> bool:
    """Create a single pool repo. Returns True on success."""
    full = f"{ORG}/{name}"

    # 1. Check existence
    if repo_exists(name):
        print(f"[skip] {full} already exists")
        return True

    if dry_run:
        print(f"[dry-run] would create {full} — {description}")
        return True

    print(f"[create] {full}")

    # 2. Create the repo on GitHub
    result = run(
        ["gh", "repo", "create", full, "--public", "--description", description],
        check=False,
    )
    if result.returncode != 0:
        print(f"  ERROR creating repo: {result.stderr.strip()}", file=sys.stderr)
        return False

    # 3. Populate in a temp directory
    tmpdir = tempfile.mkdtemp(prefix=f"pool-{name}-")
    try:
        _populate_and_push(name, description, full, tmpdir)
    except Exception as exc:
        print(f"  ERROR populating repo: {exc}", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"  done: {full}")
    return True


def _populate_and_push(name: str, description: str, full: str, tmpdir: str) -> None:
    """Initialise repo contents in tmpdir and push to remote."""
    tmp = Path(tmpdir)

    # README
    (tmp / "README.md").write_text(
        README_TEMPLATE.format(repo_name=name, description=description)
    )

    # .gitignore
    (tmp / ".gitignore").write_text(GITIGNORE)

    # Workflow
    wf_dir = tmp / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "sync.yml").write_text(SYNC_WORKFLOW)

    # pool_sync.py
    scripts_dir = tmp / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(POOL_SYNC_PATH, scripts_dir / "pool_sync.py")

    # Git init + push
    env = {**os.environ}
    cwd = str(tmp)
    run(["git", "init"], cwd=cwd)
    run(["git", "checkout", "-b", "main"], cwd=cwd)
    run(["git", "add", "."], cwd=cwd)
    run(["git", "commit", "-m", "initial commit"], cwd=cwd)
    run(["git", "remote", "add", "origin", f"https://github.com/{full}.git"], cwd=cwd)
    run(["git", "push", "-u", "origin", "main"], cwd=cwd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Create apt-gh pool repos")
    parser.add_argument("--repo", help="Create only this specific repo")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating")
    args = parser.parse_args()

    all_repos = build_repo_list()
    print(f"Loaded {len(all_repos)} repos from {REPO_MAP_PATH}")

    if args.repo:
        matched = [(n, d) for n, d in all_repos if n == args.repo]
        if not matched:
            print(f"ERROR: repo '{args.repo}' not found in repo_map.json", file=sys.stderr)
            sys.exit(1)
        targets = matched
    else:
        targets = all_repos

    if args.dry_run:
        print("=== DRY RUN ===")

    failures = 0
    for name, desc in targets:
        if not create_repo(name, desc, args.dry_run):
            failures += 1

    print(f"\nDone. {len(targets) - failures}/{len(targets)} succeeded.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
