import json

import pytest

import launch_rig
from conftest import TEST_BUCKET, FakeSession, client_error
from launch_rig import (
    Rig,
    build_user_data,
    cmd_deprovision,
    cmd_terminate,
    delete_security_group,
    ensure_bucket,
    ensure_security_group,
    find_rig_instance,
    instance_policy,
    run_instance,
    run_instance_params,
    ssm_session_command,
)
from rig_config import load_config

DEFAULT_VPC = {"ec2.describe_vpcs": {"Vpcs": [{"VpcId": "vpc-1"}]}}
NO_SECURITY_GROUPS = {"ec2.describe_security_groups": {"SecurityGroups": []}}
SUBNETS = {
    "ec2.describe_subnets": {
        "Subnets": [
            {"SubnetId": "subnet-b", "AvailabilityZone": "us-west-2b"},
            {"SubnetId": "subnet-a", "AvailabilityZone": "us-west-2a"},
        ]
    }
}


def rig_for(cfg, responses=None, tmp_path=None) -> Rig:
    state_file = (tmp_path / ".rig_state.json") if tmp_path else launch_rig.STATE_FILE
    return Rig(cfg, FakeSession(responses), state_file=state_file)


def tagged_instance(instance_id: str, name: str, **tags) -> dict:
    tag_list = [{"Key": "Name", "Value": name}, *({"Key": k, "Value": v} for k, v in tags.items())]
    return {"InstanceId": instance_id, "State": {"Name": "running"}, "Tags": tag_list}


def rig_tags(cfg: dict, name: str) -> list[dict]:
    return [*({"Key": k, "Value": v} for k, v in cfg["tags"].items()), {"Key": "Name", "Value": name}]


def rig_group(cfg: dict, group_id: str, ingress: list | None = None) -> dict:
    return {"GroupId": group_id, "Tags": rig_tags(cfg, f"{cfg['rig']['name']}-sg"), "IpPermissions": ingress or []}


@pytest.fixture
def owned_cfg() -> dict:
    env = {"RIG_OUTPUTS_BUCKET": TEST_BUCKET, "AWS_REGION": "us-west-2", "RIG_OWNER_TAG": "bob"}
    return load_config(env=env)


def test_instance_policy_is_read_only_and_scoped(cfg):
    policy = instance_policy(cfg, "ACCOUNT")
    actions = {action for statement in policy["Statement"] for action in statement["Action"]}
    assert actions == {"s3:GetObject", "s3:ListBucket", "ssm:GetParameter"}
    resources = [statement["Resource"] for statement in policy["Statement"]]
    assert f"arn:aws:s3:::{TEST_BUCKET}/bootstrap/*" in resources
    assert resources[2].endswith(":parameter/comfyui-video-rig/hf-token")


def test_user_data_sets_the_fail_safe_and_watchdog_settings(cfg):
    user_data = build_user_data(cfg)
    assert "shutdown -h +180" in user_data
    assert "RIG_IDLE_STOP_MINUTES=30" in user_data
    assert "RIG_MAX_JOB_MINUTES=360" in user_data
    assert "RIG_HF_TOKEN_PARAM=/comfyui-video-rig/hf-token" in user_data
    assert f"s3://{TEST_BUCKET}/bootstrap/" in user_data


def test_instance_stops_on_shutdown_and_has_no_key_pair(cfg):
    params = run_instance_params(cfg, "ami-1", "sg-1", "subnet-1")
    assert params["InstanceInitiatedShutdownBehavior"] == "stop"
    assert "KeyName" not in params
    assert params["SecurityGroupIds"] == ["sg-1"]
    assert params["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"] is True


def test_security_group_is_created_without_ingress_rules(cfg):
    responses = {**DEFAULT_VPC, **SUBNETS, **NO_SECURITY_GROUPS, "ec2.create_security_group": {"GroupId": "sg-new"}}
    rig = rig_for(cfg, responses)
    assert ensure_security_group(rig) == "sg-new"
    assert not [op for op in rig.session.operations() if "ingress" in op]


def test_existing_security_group_is_reused(cfg):
    existing = {"ec2.describe_security_groups": {"SecurityGroups": [rig_group(cfg, "sg-old")]}}
    rig = rig_for(cfg, {**DEFAULT_VPC, **SUBNETS, **existing})
    assert ensure_security_group(rig) == "sg-old"
    assert "ec2.create_security_group" not in rig.session.operations()


def test_existing_security_group_with_inbound_rules_is_rejected(cfg):
    ingress = [{"IpProtocol": "tcp", "FromPort": 8188, "ToPort": 8188, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    existing = {"ec2.describe_security_groups": {"SecurityGroups": [rig_group(cfg, "sg-open", ingress)]}}
    rig = rig_for(cfg, {**DEFAULT_VPC, **SUBNETS, **existing})
    with pytest.raises(SystemExit, match="inbound rules"):
        ensure_security_group(rig)


def test_existing_security_group_without_the_rig_tags_is_rejected(owned_cfg):
    foreign = {"GroupId": "sg-alice", "Tags": rig_tags({"tags": {"Owner": "alice"}}, "x"), "IpPermissions": []}
    rig = rig_for(owned_cfg, {**DEFAULT_VPC, **SUBNETS, "ec2.describe_security_groups": {"SecurityGroups": [foreign]}})
    with pytest.raises(SystemExit, match="rig tags"):
        ensure_security_group(rig)


def test_existing_bucket_is_checked_for_owner_and_gets_the_public_access_block(cfg):
    rig = rig_for(cfg, {"sts.get_caller_identity": {"Account": "111122223333"}})
    ensure_bucket(rig)
    assert rig.session.kwargs_of("s3.head_bucket") == [{"Bucket": TEST_BUCKET, "ExpectedBucketOwner": "111122223333"}]
    block = rig.session.kwargs_of("s3.put_public_access_block")
    assert block and all(block[0]["PublicAccessBlockConfiguration"].values())
    assert "s3.create_bucket" not in rig.session.operations()


def test_bucket_owned_by_another_account_stops_the_command(cfg):
    responses = {"sts.get_caller_identity": {"Account": "111122223333"}, "s3.head_bucket": client_error("403")}
    rig = rig_for(cfg, responses)
    with pytest.raises(SystemExit, match="not owned"):
        ensure_bucket(rig)
    assert "s3.put_public_access_block" not in rig.session.operations()


def test_run_instance_waits_for_profile_propagation(cfg):
    not_ready = client_error("InvalidParameterValue", "Invalid IAM Instance Profile name")
    responses = {**DEFAULT_VPC, **SUBNETS, "ec2.run_instances": [not_ready, {"Instances": [{"InstanceId": "i-1"}]}]}
    sleeps = []
    assert run_instance(rig_for(cfg, responses), "ami-1", "sg-1", sleep=sleeps.append) == "i-1"
    assert sleeps == [10]


def test_run_instance_raises_other_errors_at_once(cfg):
    responses = {**DEFAULT_VPC, **SUBNETS, "ec2.run_instances": client_error("InsufficientInstanceCapacity")}
    with pytest.raises(Exception, match="InsufficientInstanceCapacity"):
        run_instance(rig_for(cfg, responses), "ami-1", "sg-1", sleep=lambda _s: None)


def test_first_subnet_by_availability_zone_is_used(cfg):
    rig = rig_for(cfg, {**DEFAULT_VPC, **SUBNETS, "ec2.run_instances": {"Instances": [{"InstanceId": "i-1"}]}})
    run_instance(rig, "ami-1", "sg-1")
    assert rig.session.kwargs_of("ec2.run_instances")[0]["SubnetId"] == "subnet-a"


def test_state_file_instance_with_another_name_is_ignored(cfg, tmp_path):
    rig = rig_for(cfg, tmp_path=tmp_path)
    rig.state_file.write_text(json.dumps({"instance_id": "i-other"}))
    rig.session.responses["ec2.describe_instances"] = [
        {"Reservations": [{"Instances": [tagged_instance("i-other", "unrelated")]}]},
        {"Reservations": [{"Instances": [tagged_instance("i-rig", cfg["rig"]["name"])]}]},
    ]
    assert find_rig_instance(rig)["InstanceId"] == "i-rig"
    assert json.loads(rig.state_file.read_text())["instance_id"] == "i-rig"


def test_state_file_instance_with_another_owner_is_ignored(owned_cfg, tmp_path):
    name = owned_cfg["rig"]["name"]
    rig = rig_for(owned_cfg, tmp_path=tmp_path)
    rig.state_file.write_text(json.dumps({"instance_id": "i-alice"}))
    rig.session.responses["ec2.describe_instances"] = [
        {"Reservations": [{"Instances": [tagged_instance("i-alice", name, Project=name, Owner="alice")]}]},
        {"Reservations": []},
    ]
    assert find_rig_instance(rig) is None


def test_two_tagged_instances_stop_the_command(cfg, tmp_path):
    name = cfg["rig"]["name"]
    instances = [tagged_instance("i-1", name), tagged_instance("i-2", name)]
    rig = rig_for(cfg, {"ec2.describe_instances": {"Reservations": [{"Instances": instances}]}}, tmp_path)
    with pytest.raises(SystemExit, match="multiple instances"):
        find_rig_instance(rig)


def test_terminate_needs_the_typed_confirmation(cfg, tmp_path, monkeypatch):
    instance = tagged_instance("i-rig", cfg["rig"]["name"])
    rig = rig_for(cfg, {"ec2.describe_instances": {"Reservations": [{"Instances": [instance]}]}}, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    with pytest.raises(SystemExit, match="aborted"):
        cmd_terminate(rig)
    assert "ec2.terminate_instances" not in rig.session.operations()


def test_ssm_commands_rely_on_the_ambient_profile(cfg):
    argv = ssm_session_command(cfg, "i-1", port_forward=True)
    assert "--profile" not in argv
    assert "portNumber=8188,localPortNumber=8188" in argv
    assert argv[-2:] == ["--region", "us-west-2"]


def test_deprovision_treats_missing_resources_as_done(cfg, tmp_path):
    gone = client_error("NoSuchEntity")
    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        "ec2.describe_instances": {"Reservations": []},
        "iam.remove_role_from_instance_profile": gone,
        "iam.delete_instance_profile": gone,
        "ssm.delete_parameter": client_error("ParameterNotFound"),
        "ec2.describe_security_groups": {"SecurityGroups": [{"GroupId": "sg-1"}]},
    }
    rig = rig_for(cfg, responses, tmp_path)
    cmd_deprovision(rig)
    assert rig.session.kwargs_of("ec2.delete_security_group") == [{"GroupId": "sg-1"}]


def test_deprovision_ignores_a_state_file_group_without_the_rig_tags(owned_cfg, tmp_path):
    def describe_security_groups(**kwargs):
        if kwargs.get("GroupIds") == ["sg-stale"]:
            return {"SecurityGroups": []}
        return {"SecurityGroups": [rig_group(owned_cfg, "sg-rig")]}

    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        "ec2.describe_instances": {"Reservations": []},
        "ec2.describe_security_groups": describe_security_groups,
    }
    rig = rig_for(owned_cfg, responses, tmp_path)
    rig.state_file.write_text(json.dumps({"sg_id": "sg-stale"}))
    cmd_deprovision(rig)
    assert rig.session.kwargs_of("ec2.delete_security_group") == [{"GroupId": "sg-rig"}]
    filters = {f["Name"]: f["Values"] for f in rig.session.kwargs_of("ec2.describe_security_groups")[0]["Filters"]}
    assert filters["vpc-id"] == ["vpc-1"]
    assert filters["tag:Owner"] == ["bob"]


def test_deprovision_reports_real_failures(cfg, tmp_path):
    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        "ec2.describe_instances": {"Reservations": []},
        "iam.delete_role": client_error("DeleteConflict"),
        **NO_SECURITY_GROUPS,
    }
    with pytest.raises(SystemExit, match="deprovision incomplete"):
        cmd_deprovision(rig_for(cfg, responses, tmp_path))


def test_security_group_delete_retries_while_enis_release():
    session = FakeSession({"ec2.delete_security_group": [client_error("DependencyViolation"), {}]})
    sleeps = []
    assert delete_security_group(session.client("ec2"), "sg-1", sleep=sleeps.append)
    assert sleeps == [15]
