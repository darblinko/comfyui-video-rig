#!/usr/bin/env python3
"""Download model weights listed in models.json into the local models directory.

Runs on the rig with the ComfyUI venv python (which has huggingface_hub).
Per-file failures are logged and skipped so one bad manifest entry does not
kill the whole bootstrap; exits non-zero only if every requested file failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download


def download_file(entry: dict, dest_root: Path, staging: Path) -> bool:
    dest_dir = dest_root / entry["dest"]
    dest_path = dest_dir / Path(entry["path"]).name
    if dest_path.exists():
        print(f"exists, skipping: {dest_path}")
        return True
    print(f"downloading {entry['repo']} :: {entry['path']}")
    downloaded = hf_hub_download(repo_id=entry["repo"], filename=entry["path"], local_dir=staging)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(downloaded, dest_path)
    print(f"  -> {dest_path}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--groups", required=True, help="comma-separated group names")
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    staging = args.dest / "_staging"

    ok, failed = 0, []
    for group in groups:
        if group not in manifest:
            print(f"WARNING: group '{group}' not in manifest, skipping")
            continue
        note = manifest[group].get("note")
        if note:
            print(f"[{group}] {note}")
        for entry in manifest[group]["files"]:
            try:
                download_file(entry, args.dest, staging)
                ok += 1
            except Exception as err:
                # One bad manifest entry must not stop the remaining downloads.
                failed.append(f"{entry['repo']}::{entry['path']}")
                print(f"FAILED {entry['repo']} :: {entry['path']}: {err}")

    shutil.rmtree(staging, ignore_errors=True)
    print(f"\ndownload summary: {ok} ok, {len(failed)} failed")
    for item in failed:
        print(f"  failed: {item}")
    if failed and ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
