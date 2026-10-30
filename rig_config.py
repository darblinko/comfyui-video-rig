"""Load rig.config.toml and apply the account-specific environment overrides.

Account-specific values never live in the committed config:

  AWS_PROFILE         read by boto3 and the AWS CLI directly; unset or empty means the default chain
  AWS_REGION          overrides [aws] region
  RIG_OUTPUTS_BUCKET  overrides [s3] outputs_bucket; one of the two must be set
  RIG_OWNER_TAG       adds an Owner tag to every rig resource
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, MutableMapping
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
CONFIG_FILE = REPO_DIR / "rig.config.toml"
BUCKET_PLACEHOLDER = "<RIG_OUTPUTS_BUCKET>"


def load_config(path: Path = CONFIG_FILE, env: Mapping[str, str] | None = None, require_bucket: bool = True) -> dict:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return apply_env_overrides(raw, os.environ if env is None else env, require_bucket)


def apply_env_overrides(cfg: dict, env: Mapping[str, str], require_bucket: bool = True) -> dict:
    """Return a copy of cfg with region, bucket and owner tag taken from env when set.

    With require_bucket=False a missing bucket becomes BUCKET_PLACEHOLDER, so a
    dry run works on a fresh clone with no environment."""
    region = env.get("AWS_REGION") or cfg["aws"]["region"]
    bucket = env.get("RIG_OUTPUTS_BUCKET") or cfg["s3"]["outputs_bucket"]
    if not bucket:
        if require_bucket:
            raise SystemExit("set RIG_OUTPUTS_BUCKET (or [s3] outputs_bucket in rig.config.toml)")
        bucket = BUCKET_PLACEHOLDER
    tags = dict(cfg.get("tags", {}))
    if env.get("RIG_OWNER_TAG"):
        tags["Owner"] = env["RIG_OWNER_TAG"]
    return {
        **cfg,
        "aws": {**cfg["aws"], "region": region},
        "s3": {**cfg["s3"], "outputs_bucket": bucket},
        "tags": tags,
    }


def drop_empty_aws_profile(env: MutableMapping[str, str]) -> None:
    """Remove empty AWS_PROFILE and AWS_DEFAULT_PROFILE. boto3 treats an empty
    value as a profile named "" and raises ProfileNotFound; unset means the
    default credential chain, which is what an empty value in .env intends."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        if name in env and not env[name].strip():
            del env[name]


def aws_session(region: str):
    """boto3 session for region; profile_name stays unset so boto3 honours
    AWS_PROFILE or the default chain."""
    import boto3

    drop_empty_aws_profile(os.environ)
    return boto3.Session(region_name=region)
