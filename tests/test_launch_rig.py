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
    ensure_role,
    ensure_security_group,
    find_rig_instance,
    run_instance,
    run_instance_params,
    ssm_session_command,
    store_hf_token,
)
from rig_config import load_config
from rig_iam import EC2_TRUST_POLICY, instance_policy, ownership_tags, tag_pairs

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


# The ssm:GetParameter* grant of AmazonSSMManagedInstanceCore (AWS managed policy reference).
SSM_CORE_PARAMETER_GRANT = {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"], "Resource": "*"}
TOKEN_ARN = "arn:aws:ssm:us-west-2:ACCOUNT:parameter/comfyui-video-rig/hf-token"


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def statement_matches(statement: dict, action: str, resource: str) -> bool:
    if action not in as_list(statement["Action"]):
        return False
    if "NotResource" in statement:
        return resource not in as_list(statement["NotResource"])
    return any(r == "*" or r == resource for r in as_list(statement["Resource"]))


def allowed(statements: list[dict], action: str, resource: str) -> bool:
    """IAM evaluation for unconditioned statements: an explicit Deny wins over any Allow."""
    matching = [st for st in statements if "Condition" not in st and statement_matches(st, action, resource)]
    return any(st["Effect"] == "Allow" for st in matching) and not any(st["Effect"] == "Deny" for st in matching)


def test_instance_policy_allows_only_bootstrap_reads_and_the_token(cfg):
    policy = instance_policy(cfg, "ACCOUNT")
    allows = [st for st in policy["Statement"] if st["Effect"] == "Allow"]
    assert {action for st in allows for action in st["Action"]} == {"s3:GetObject", "s3:ListBucket", "ssm:GetParameter"}
    assert f"arn:aws:s3:::{TEST_BUCKET}/bootstrap/*" in [st["Resource"] for st in allows]


def test_combined_policies_read_only_the_rig_token_parameter(cfg):
    statements = [SSM_CORE_PARAMETER_GRANT, *instance_policy(cfg, "ACCOUNT")["Statement"]]
    other = "arn:aws:ssm:us-west-2:ACCOUNT:parameter/prod/db-password"
    assert allowed(statements, "ssm:GetParameter", TOKEN_ARN)
    assert not allowed(statements, "ssm:GetParameter", other)
    assert not allowed(statements, "ssm:GetParameters", other)
    assert not allowed(statements, "ssm:GetParameters", TOKEN_ARN.replace("hf-token", "other"))


def owned_iam_responses(cfg: dict, profile_roles=("comfyui-video-rig-role",)) -> dict:
    role, profile, param = "comfyui-video-rig-role", "comfyui-video-rig-profile", "/comfyui-video-rig/hf-token"
    return {
        "sts.get_caller_identity": {"Account": "111122223333"},
        "iam.get_role": {
            "Role": {"Tags": tag_pairs(ownership_tags(cfg, role)), "AssumeRolePolicyDocument": EC2_TRUST_POLICY}
        },
        "iam.get_instance_profile": {
            "InstanceProfile": {
                "Roles": [{"RoleName": name} for name in profile_roles],
                "Tags": tag_pairs(ownership_tags(cfg, profile)),
            }
        },
        "ssm.list_tags_for_resource": {"TagList": tag_pairs(ownership_tags(cfg, param))},
    }


NO_IAM = {
    "sts.get_caller_identity": {"Account": "111122223333"},
    "iam.get_role": client_error("NoSuchEntity"),
    "iam.get_instance_profile": client_error("NoSuchEntity"),
    "ssm.list_tags_for_resource": client_error("InvalidResourceId"),
}
IAM_WRITES = ("iam.attach_role_policy", "iam.put_role_policy", "iam.add_role_to_instance_profile")


def test_new_role_and_profile_carry_the_ownership_tags(cfg):
    rig = rig_for(cfg, NO_IAM)
    ensure_role(rig)
    for operation, name in (("iam.create_role", "comfyui-video-rig-role"),
                            ("iam.create_instance_profile", "comfyui-video-rig-profile")):  # fmt: skip
        tags = {t["Key"]: t["Value"] for t in rig.session.kwargs_of(operation)[0]["Tags"]}
        assert tags == {"Project": "comfyui-video-rig", "Name": name, "RigBucket": TEST_BUCKET}
    assert rig.session.kwargs_of("iam.add_role_to_instance_profile")


def test_owned_role_is_updated(cfg):
    rig = rig_for(cfg, owned_iam_responses(cfg))
    ensure_role(rig)
    assert "iam.create_role" not in rig.session.operations()
    assert rig.session.kwargs_of("iam.put_role_policy")
    assert "iam.add_role_to_instance_profile" not in rig.session.operations()


def test_role_of_another_deployment_is_not_changed(cfg):
    responses = owned_iam_responses(cfg)
    other = {**cfg, "s3": {**cfg["s3"], "outputs_bucket": "alices-bucket"}}
    responses["iam.get_role"]["Role"]["Tags"] = tag_pairs(ownership_tags(other, "comfyui-video-rig-role"))
    rig = rig_for(cfg, responses)
    with pytest.raises(SystemExit, match="lacks the rig tags"):
        ensure_role(rig)
    assert not [op for op in rig.session.operations() if op in IAM_WRITES]


def test_role_trusting_another_principal_is_not_changed(cfg):
    responses = owned_iam_responses(cfg)
    trust = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::444455556666:root"},
                            "Action": "sts:AssumeRole"}]}  # fmt: skip
    responses["iam.get_role"]["Role"]["AssumeRolePolicyDocument"] = trust
    rig = rig_for(cfg, responses)
    with pytest.raises(SystemExit, match="other than EC2"):
        ensure_role(rig)
    assert not [op for op in rig.session.operations() if op in IAM_WRITES]


def test_profile_holding_an_unrelated_role_is_refused(cfg):
    rig = rig_for(cfg, owned_iam_responses(cfg, profile_roles=("admin-role",)))
    with pytest.raises(SystemExit, match="holds role admin-role"):
        ensure_role(rig)
    assert "iam.add_role_to_instance_profile" not in rig.session.operations()


def test_new_token_parameter_is_tagged(cfg):
    rig = rig_for(cfg, NO_IAM)
    store_hf_token(rig, "hf_x")
    (put,) = rig.session.kwargs_of("ssm.put_parameter")
    assert "Overwrite" not in put
    assert {"Key": "RigBucket", "Value": TEST_BUCKET} in put["Tags"]


def test_token_parameter_of_another_deployment_is_not_overwritten(cfg):
    responses = {**owned_iam_responses(cfg), "ssm.list_tags_for_resource": {"TagList": []}}
    rig = rig_for(cfg, responses)
    with pytest.raises(SystemExit, match="lacks the rig tags"):
        store_hf_token(rig, "hf_x")
    assert "ssm.put_parameter" not in rig.session.operations()


def test_user_data_installs_a_per_boot_fail_safe_before_any_download(cfg):
    user_data = build_user_data(cfg)
    per_boot = user_data.index("/var/lib/cloud/scripts/per-boot/rig-fail-safe.sh")
    assert per_boot < user_data.index("aws s3 cp")
    assert "systemctl is-enabled --quiet rig-idle-watchdog.timer" in user_data


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
    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        **NO_IAM,
        "ec2.describe_instances": {"Reservations": []},
        "ec2.describe_security_groups": {"SecurityGroups": [{"GroupId": "sg-1"}]},
    }
    rig = rig_for(cfg, responses, tmp_path)
    cmd_deprovision(rig)
    assert rig.session.kwargs_of("ec2.delete_security_group") == [{"GroupId": "sg-1"}]
    assert not [op for op in rig.session.operations() if op.startswith("iam.delete") or op == "ssm.delete_parameter"]


def test_deprovision_tolerates_resources_deleted_meanwhile(cfg, tmp_path):
    gone = client_error("NoSuchEntity")
    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        **owned_iam_responses(cfg),
        "ec2.describe_instances": {"Reservations": []},
        "iam.remove_role_from_instance_profile": gone,
        "iam.delete_instance_profile": gone,
        "ssm.delete_parameter": client_error("ParameterNotFound"),
        **NO_SECURITY_GROUPS,
    }
    rig = rig_for(cfg, responses, tmp_path)
    cmd_deprovision(rig)
    assert rig.session.kwargs_of("iam.delete_role") == [{"RoleName": "comfyui-video-rig-role"}]


def test_deprovision_leaves_another_deployments_iam_and_token_alone(cfg, tmp_path):
    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        **owned_iam_responses(cfg, profile_roles=("comfyui-video-rig-role", "admin-role")),
        "ec2.describe_instances": {"Reservations": []},
        "ssm.list_tags_for_resource": {"TagList": []},
        **NO_SECURITY_GROUPS,
    }
    responses["iam.get_role"]["Role"]["Tags"] = []
    rig = rig_for(cfg, responses, tmp_path)
    with pytest.raises(SystemExit, match="deprovision incomplete"):
        cmd_deprovision(rig)
    touched = [op for op in rig.session.operations() if op.startswith(("iam.delete", "iam.detach", "iam.remove"))]
    assert touched == []
    assert "ssm.delete_parameter" not in rig.session.operations()


def test_deprovision_ignores_a_state_file_group_without_the_rig_tags(owned_cfg, tmp_path):
    def describe_security_groups(**kwargs):
        if kwargs.get("GroupIds") == ["sg-stale"]:
            return {"SecurityGroups": []}
        return {"SecurityGroups": [rig_group(owned_cfg, "sg-rig")]}

    responses = {
        **DEFAULT_VPC,
        **SUBNETS,
        **NO_IAM,
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
        **owned_iam_responses(cfg),
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
