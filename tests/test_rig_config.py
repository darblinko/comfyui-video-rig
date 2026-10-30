import copy

import pytest

from rig_config import BUCKET_PLACEHOLDER, CONFIG_FILE, apply_env_overrides, load_config


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
