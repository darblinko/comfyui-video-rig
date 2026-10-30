from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from dry_run import REDACTED, DryRunSession, format_call, placeholder_responses


def test_put_parameter_value_is_redacted():
    text = format_call("ssm", "put_parameter", (), {"Name": "/x", "Value": "secret-value", "Type": "SecureString"})
    assert "secret-value" not in text
    assert REDACTED in text


def test_json_policy_strings_are_pretty_printed():
    text = format_call("iam", "put_role_policy", (), {"PolicyDocument": '{"Version": "2012-10-17", "Statement": []}'})
    assert "PolicyDocument (JSON string)" in text


def test_multi_line_strings_print_as_a_block():
    text = format_call("ec2", "run_instances", (), {"UserData": "#!/bin/bash\necho hi\n"})
    assert "UserData: |\n        #!/bin/bash\n        echo hi" in text


def test_paths_under_the_repo_print_relative():
    root = Path("/repo")
    text = format_call("s3", "upload_file", (), {"Filename": "/repo/bootstrap/a.sh"}, root)
    assert "Filename: bootstrap/a.sh" in text


def test_session_prints_calls_and_returns_placeholders(cfg):
    lines = []
    session = DryRunSession(placeholder_responses(cfg, resources_exist=False, instance_exists=False), out=lines.append)
    s3 = session.client("s3")
    with pytest.raises(ClientError):
        s3.head_bucket(Bucket="b")
    assert session.client("sts").get_caller_identity()["Account"] == "<ACCOUNT_ID>"
    assert lines[0].startswith("[dry-run] s3.head_bucket")


def test_modeled_exceptions_are_client_errors(cfg):
    session = DryRunSession({}, out=lambda _line: None)
    assert issubclass(session.client("iam").exceptions.EntityAlreadyExistsException, ClientError)


def test_ami_lookup_and_missing_token_parameter(cfg):
    session = DryRunSession(placeholder_responses(cfg, resources_exist=True, instance_exists=True), out=print)
    ssm = session.client("ssm")
    assert ssm.get_parameter(Name=cfg["rig"]["ami_ssm_parameter"])["Parameter"]["Value"] == "ami-<latest>"
    with pytest.raises(ClientError):
        ssm.get_parameter(Name="/comfyui-video-rig/hf-token")
