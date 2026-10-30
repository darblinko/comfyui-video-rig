import copy
import os

import pytest

import launch_rig
from conftest import REPO_DIR
from rig_config import (
    BUCKET_PLACEHOLDER,
    CONFIG_FILE,
    apply_env_overrides,
    aws_session,
    drop_empty_aws_profile,
    load_config,
)


def test_committed_config_has_no_bucket_or_profile():
    text = CONFIG_FILE.read_text()
    assert 'outputs_bucket = ""' in text
    assert "profile" not in load_config(env={"RIG_OUTPUTS_BUCKET": "b"})["aws"]


def test_env_overrides_region_bucket_and_owner_tag():
    cfg = load_config(env={"AWS_REGION": "eu-west-1", "RIG_OUTPUTS_BUCKET": "my-bucket", "RIG_OWNER_TAG": "ops"})
    assert cfg["aws"]["region"] == "eu-west-1"
    assert cfg["s3"]["outputs_bucket"] == "my-bucket"
    assert cfg["tags"] == {"Project": "comfyui-video-rig", "Owner": "ops"}


def test_missing_bucket_stops_a_real_command():
    with pytest.raises(SystemExit, match="RIG_OUTPUTS_BUCKET"):
        load_config(env={})


def test_missing_bucket_becomes_a_placeholder_for_dry_runs():
    cfg = load_config(env={}, require_bucket=False)
    assert cfg["s3"]["outputs_bucket"] == BUCKET_PLACEHOLDER


def test_overrides_return_a_new_dict():
    raw = {"aws": {"region": "us-west-2"}, "s3": {"outputs_bucket": "a"}, "tags": {"Project": "p"}}
    before = copy.deepcopy(raw)
    apply_env_overrides(raw, {"RIG_OUTPUTS_BUCKET": "b", "RIG_OWNER_TAG": "o"})
    assert raw == before


@pytest.mark.parametrize(
    "make_session",
    [lambda: aws_session("us-west-2"), lambda: launch_rig.aws_session({"aws": {"region": "us-west-2"}})],
)
def test_an_empty_aws_profile_falls_back_to_the_default_chain(make_session, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "")
    make_session().client("s3")
    assert "AWS_PROFILE" not in os.environ


def test_a_set_aws_profile_is_left_for_boto3(monkeypatch):
    env = {"AWS_PROFILE": "work", "AWS_DEFAULT_PROFILE": ""}
    drop_empty_aws_profile(env)
    assert env == {"AWS_PROFILE": "work"}


def test_env_template_leaves_every_optional_value_commented_out():
    lines = (REPO_DIR / ".env.example").read_text().splitlines()
    assigned = [line.split("=")[0] for line in lines if line and not line.startswith("#")]
    assert assigned == ["RIG_OUTPUTS_BUCKET"]
