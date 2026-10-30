#!/usr/bin/env python3
"""Download model weights listed in models.json into the local models directory.

Runs on the rig with the ComfyUI venv python (which has huggingface_hub).
Per-file failures are logged and skipped so one bad manifest entry does not
kill the whole bootstrap; exits non-zero only if every requested file failed.

Each manifest entry may pin "revision" (a commit SHA) and "sha256". The
expected size, the LFS sha256 and the resolved commit come from the Hub's file
metadata at that revision. A cached file is kept only if its size matches (and
its sha256, when pinned); otherwise it is downloaded again. A download is moved
into place only after its size and sha256 check out. The resolved revision,
size and sha256 of every file are written to <dest>/models.lock.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

SHA256_RE = re.compile(r"[0-9a-f]{64}")
CHUNK_BYTES = 16 * 1024 * 1024


def hf_fetch(repo_id: str, filename: str, local_dir: Path, revision: str | None = None) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision)


def remote_metadata(entry: dict) -> dict:
    """Size, LFS sha256 (None for non-LFS files) and commit of the file at the entry's revision."""
    from huggingface_hub import get_hf_file_metadata, hf_hub_url

    meta = get_hf_file_metadata(hf_hub_url(entry["repo"], entry["path"], revision=entry.get("revision")))
    etag = (meta.etag or "").strip('"')
    return {"size": meta.size, "sha256": etag if SHA256_RE.fullmatch(etag) else None, "commit": meta.commit_hash}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def verification_error(path: Path, size: int | None, sha256: str | None) -> str | None:
    actual_size = path.stat().st_size
    if size is not None and actual_size != size:
        return f"size {actual_size} != expected {size}"
    if sha256 is not None and file_sha256(path) != sha256:
        return f"sha256 does not match {sha256}"
    return None


def download_file(entry: dict, dest_root: Path, staging: Path, fetch=None, metadata=None) -> dict:
    """Ensure one verified file is in place and return its lock record."""
    fetch = fetch or hf_fetch
    metadata = metadata or remote_metadata
    dest_path = dest_root / entry["dest"] / Path(entry["path"]).name
    meta = metadata(entry)
    pinned_sha = entry.get("sha256")
    record = {
        "repo": entry["repo"],
        "path": entry["path"],
        "revision": meta["commit"],
        "size": meta["size"],
        "sha256": pinned_sha or meta["sha256"],
    }
    if dest_path.exists():
        problem = verification_error(dest_path, meta["size"], pinned_sha)
        if problem is None:
            print(f"exists, verified by {'sha256' if pinned_sha else 'size'}: {dest_path}")
            return record
        print(f"cached file failed verification ({problem}), downloading again: {dest_path}")
        dest_path.unlink()
    print(f"downloading {entry['repo']} :: {entry['path']}")
    downloaded = Path(fetch(entry["repo"], entry["path"], staging, revision=entry.get("revision")))
    problem = verification_error(downloaded, meta["size"], record["sha256"])
    if problem is not None:
        downloaded.unlink(missing_ok=True)
        raise RuntimeError(f"download failed verification: {problem}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(downloaded, dest_path)
    print(f"  saved {dest_path}")
    return record


def download_groups(manifest: dict, groups: list[str], dest: Path) -> tuple[dict, list[str]]:
    staging = dest / "_staging"
    records, failed = {}, []
    for group in groups:
        if group not in manifest:
            print(f"WARNING: group '{group}' not in manifest, skipping")
            continue
        note = manifest[group].get("note")
        if note:
            print(f"[{group}] {note}")
        for entry in manifest[group]["files"]:
            key = f"{entry['repo']}::{entry['path']}"
            try:
                records[key] = download_file(entry, dest, staging)
            except Exception as err:
                # One bad manifest entry must not stop the remaining downloads.
                failed.append(key)
                print(f"FAILED {entry['repo']} :: {entry['path']}: {err}")
    shutil.rmtree(staging, ignore_errors=True)
    return records, failed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--groups", required=True, help="comma-separated group names")
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    args.dest.mkdir(parents=True, exist_ok=True)
    records, failed = download_groups(manifest, groups, args.dest)
    (args.dest / "models.lock.json").write_text(json.dumps(records, indent=2) + "\n")

    print(f"\ndownload summary: {len(records)} ok, {len(failed)} failed")
    for item in failed:
        print(f"  failed: {item}")
    if failed and not records:
        sys.exit(1)


if __name__ == "__main__":
    main()
