#!/usr/bin/env python3
"""Upload a run folder to the outputs bucket and print presigned share links.

Standalone:
  python3 runner/s3_sync.py outputs/20260710-153000-shaker_demo

Also called by run_job.py unless --no-sync is passed.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rig_config import CONFIG_FILE, aws_session, load_config

PRESIGN_TTL_S = 7 * 24 * 3600  # SigV4 maximum
SHAREABLE_SUFFIXES = {".mp4", ".webm", ".webp", ".png", ".jpg", ".jpeg", ".json"}


def load_aws_config(config_path: Path = CONFIG_FILE) -> dict:
    cfg = load_config(config_path)
    return {"region": cfg["aws"]["region"], "bucket": cfg["s3"]["outputs_bucket"]}


def sync_run(run_dir: Path, aws: dict, prefix: str = "runs") -> dict[str, str]:
    """Upload every file under run_dir; returns {relative_path: presigned_url}."""
    session = aws_session(aws["region"])
    s3 = session.client("s3")
    urls: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        key = f"{prefix}/{run_dir.name}/{rel}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        s3.upload_file(str(path), aws["bucket"], key, ExtraArgs={"ContentType": content_type})
        if path.suffix.lower() in SHAREABLE_SUFFIXES:
            urls[str(rel)] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": aws["bucket"], "Key": key},
                ExpiresIn=PRESIGN_TTL_S,
            )
    return urls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    args = parser.parse_args()
    if not args.run_dir.is_dir():
        raise SystemExit(f"not a directory: {args.run_dir}")

    aws = load_aws_config(args.config)
    urls = sync_run(args.run_dir, aws)
    print(f"synced {args.run_dir.name} to s3://{aws['bucket']}/runs/{args.run_dir.name}/")
    print("share links (up to 7 days; capped by your AWS session lifetime for SSO/assumed-role profiles):")
    for rel, url in urls.items():
        print(f"  {rel}\n    {url}")


if __name__ == "__main__":
    main()
