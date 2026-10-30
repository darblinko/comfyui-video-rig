import pytest

import launch_rig


@pytest.fixture
def no_account(monkeypatch):
    for name in ("RIG_OUTPUTS_BUCKET", "AWS_PROFILE", "AWS_REGION", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(launch_rig, "aws_session", lambda _cfg: pytest.fail("dry run created a boto3 session"))
    monkeypatch.setattr(launch_rig.os, "execvp", lambda *_a: pytest.fail("dry run exec'd a command"))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("dry run asked for input"))


def state_snapshot():
    path = launch_rig.STATE_FILE
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize("command", sorted(launch_rig.COMMANDS))
def test_every_command_runs_in_dry_run_mode(command, no_account, capsys):
    before = state_snapshot()
    launch_rig.main(["--dry-run", command])
    out = capsys.readouterr().out
    assert out.startswith("dry run: no AWS call is made.")
    assert state_snapshot() == before


def test_dry_run_launch_prints_the_instance_request(no_account, monkeypatch, capsys):
    monkeypatch.setenv("HF_TOKEN", "hf-token-value-for-test")
    launch_rig.main(["--dry-run", "launch"])
    out = capsys.readouterr().out
    assert "[dry-run] ec2.run_instances" in out
    assert "InstanceInitiatedShutdownBehavior: stop" in out
    assert "shutdown -h +180" in out
    assert "hf-token-value-for-test" not in out
    assert "Value: <redacted>" in out
    assert "ingress" not in out
    assert "would write .rig_state.json" in out


def test_dry_run_tunnel_prints_the_exec_command(no_account, capsys):
    launch_rig.main(["--dry-run", "tunnel"])
    assert "[dry-run] exec: aws ssm start-session --target 'i-<rig>'" in capsys.readouterr().out
