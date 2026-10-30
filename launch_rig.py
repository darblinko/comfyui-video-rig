#!/usr/bin/env python3
"""Launch and manage the ComfyUI video GPU rig.

All knobs live in rig.config.toml; account-specific values come from the
environment (AWS_PROFILE, AWS_REGION, RIG_OUTPUTS_BUCKET, RIG_OWNER_TAG).

No inbound ports are ever opened; all access goes through SSM.
If the HF_TOKEN env var is set at launch, it is stored as an SSM SecureString
(/<rig name>/hf-token) that the instance reads for gated Hugging Face downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from dry_run import DryRunSession, placeholder_responses
from rig_config import REPO_DIR, load_config

STATE_FILE = REPO_DIR / ".rig_state.json"
GPU_QUOTA_CODE = "L-DB2E81BA"  # Running On-Demand G and VT instances (vCPUs)
SSM_MANAGED_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
INLINE_POLICY_NAME = "rig-instance-access"

COMMAND_HELP = """commands:
  provision    Create the S3 outputs bucket, IAM role/instance profile, security group
  launch       Launch the GPU instance (uploads bootstrap/ to S3 first)
  status       Show instance state, IPs, launch time
  stop         Stop the instance (compute billing stops, EBS and models persist)
  start        Start a stopped instance
  terminate    Terminate the instance and its EBS volume (asks for confirmation)
  tunnel       SSM port-forward to ComfyUI (http://localhost:<port>)
  session      Interactive SSM shell on the instance
  deprovision  Delete the SG, IAM role/profile and HF token parameter (bucket is kept)
"""

EC2_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


@dataclass(frozen=True)
class Rig:
    """The config plus the AWS session every command acts through.

    A dry-run Rig carries a DryRunSession, never touches the state file and
    prints the commands it would exec instead of running them."""

    cfg: dict
    session: object
    dry_run: bool = False
    state_file: Path = STATE_FILE


def aws_session(cfg: dict) -> boto3.Session:
    # profile_name is left unset so boto3 honours AWS_PROFILE or the default chain.
    return boto3.Session(region_name=cfg["aws"]["region"])


def resource_names(cfg: dict) -> dict:
    name = cfg["rig"]["name"]
    return {
        "role": f"{name}-role",
        "profile": f"{name}-profile",
        "sg": f"{name}-sg",
        "hf_token_param": f"/{name}/hf-token",
    }


def read_state(rig: Rig) -> dict:
    if not rig.dry_run and rig.state_file.exists():
        return json.loads(rig.state_file.read_text())
    return {}


def write_state(rig: Rig, state: dict) -> None:
    if rig.dry_run:
        print(f"[dry-run] would write {rig.state_file.name}: {json.dumps(state)}")
        return
    rig.state_file.write_text(json.dumps(state, indent=2) + "\n")


def exec_command(rig: Rig, argv: list[str]) -> None:
    if rig.dry_run:
        print(f"[dry-run] exec: {shlex.join(argv)}")
        return
    os.execvp(argv[0], argv)


def tag_list(cfg: dict, name: str) -> list[dict]:
    tags = [{"Key": k, "Value": v} for k, v in cfg.get("tags", {}).items()]
    tags.append({"Key": "Name", "Value": name})
    return tags


# --- provision -----------------------------------------------------------


PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}


def ensure_bucket(rig: Rig) -> None:
    """Create the bucket or confirm this account owns it, then block public
    access either way, so an existing bucket gets the same protection."""
    s3 = rig.session.client("s3")
    bucket = rig.cfg["s3"]["outputs_bucket"]
    region = rig.cfg["aws"]["region"]
    account_id = rig.session.client("sts").get_caller_identity()["Account"]
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account_id)
        print(f"bucket exists: s3://{bucket}")
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in ("403", "AccessDenied", "Forbidden"):
            raise SystemExit(
                f"bucket {bucket} is not owned by account {account_id} or denies access: choose another name"
            ) from err
        if code not in ("404", "NoSuchBucket"):
            raise
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        print(f"created bucket: s3://{bucket}")
    s3.put_public_access_block(
        Bucket=bucket, ExpectedBucketOwner=account_id, PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK
    )
    print(f"public access blocked: s3://{bucket}")


def instance_policy(cfg: dict, account_id: str) -> dict:
    bucket = cfg["s3"]["outputs_bucket"]
    region = cfg["aws"]["region"]
    token_param = resource_names(cfg)["hf_token_param"]
    # The instance only ever reads its bootstrap files; run outputs are synced
    # from the workstation. No PutObject: a compromised box must not be able to
    # overwrite bootstrap/bootstrap.sh, which the next launch runs as root.
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BootstrapRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/bootstrap/*",
            },
            {
                "Sid": "BootstrapList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": "bootstrap/*"}},
            },
            {
                "Sid": "HfToken",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter"],
                "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter{token_param}",
            },
        ],
    }


def ensure_role(rig: Rig) -> str:
    iam = rig.session.client("iam")
    names = resource_names(rig.cfg)
    account_id = rig.session.client("sts").get_caller_identity()["Account"]
    try:
        iam.create_role(
            RoleName=names["role"],
            AssumeRolePolicyDocument=json.dumps(EC2_TRUST_POLICY),
            Description="ComfyUI video rig instance role",
        )
        print(f"created role: {names['role']}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"role exists: {names['role']}")
    iam.attach_role_policy(RoleName=names["role"], PolicyArn=SSM_MANAGED_POLICY)
    iam.put_role_policy(
        RoleName=names["role"],
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(instance_policy(rig.cfg, account_id)),
    )
    try:
        iam.create_instance_profile(InstanceProfileName=names["profile"])
        print(f"created instance profile: {names['profile']}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"instance profile exists: {names['profile']}")
    attached = iam.get_instance_profile(InstanceProfileName=names["profile"])
    if not attached["InstanceProfile"]["Roles"]:
        iam.add_role_to_instance_profile(InstanceProfileName=names["profile"], RoleName=names["role"])
    return names["profile"]


def resolve_vpc_and_subnet(ec2, cfg: dict) -> tuple[str, str]:
    subnet_id = cfg["rig"]["subnet_id"]
    if subnet_id:
        subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        return subnet["VpcId"], subnet_id
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit(
            "No default VPC in this region. Set rig.subnet_id in rig.config.toml to a public subnet of another VPC."
        )
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    return vpc_id, sorted(subnets, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]


def check_existing_security_group(cfg: dict, group: dict) -> str:
    """Reuse an existing group only if it is the rig's and still has no inbound rules."""
    group_id = group["GroupId"]
    if not has_rig_tags(instance_tags(group), cfg, resource_names(cfg)["sg"]):
        raise SystemExit(f"security group {group_id} has the rig's name but not the rig tags: refusing to reuse it")
    if group.get("IpPermissions"):
        raise SystemExit(
            f"security group {group_id} has inbound rules; the rig needs none. "
            f"Remove them (aws ec2 revoke-security-group-ingress) or run deprovision, then retry."
        )
    print(f"security group exists (no inbound rules): {group_id}")
    return group_id


def ensure_security_group(rig: Rig) -> str:
    ec2 = rig.session.client("ec2")
    names = resource_names(rig.cfg)
    vpc_id, _ = resolve_vpc_and_subnet(ec2, rig.cfg)
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [names["sg"]]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing:
        return check_existing_security_group(rig.cfg, existing[0])
    sg = ec2.create_security_group(
        GroupName=names["sg"],
        Description="ComfyUI video rig: no inbound rules, SSM access only",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list(rig.cfg, names["sg"])}],
    )
    print(f"created security group (no inbound rules): {sg['GroupId']}")
    return sg["GroupId"]


def cmd_provision(rig: Rig) -> None:
    ensure_bucket(rig)
    ensure_role(rig)
    ensure_security_group(rig)
    print("provision complete. Next: python3 launch_rig.py launch")


# --- launch --------------------------------------------------------------


def resolve_ami(rig: Rig) -> str:
    if rig.cfg["rig"]["ami_id"]:
        return rig.cfg["rig"]["ami_id"]
    param = rig.session.client("ssm").get_parameter(Name=rig.cfg["rig"]["ami_ssm_parameter"])
    return param["Parameter"]["Value"]


def check_gpu_quota(rig: Rig) -> None:
    instance_type = rig.cfg["rig"]["instance_type"]
    try:
        ec2 = rig.session.client("ec2")
        itype = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
        needed = itype["VCpuInfo"]["DefaultVCpus"]
        quota = rig.session.client("service-quotas").get_service_quota(ServiceCode="ec2", QuotaCode=GPU_QUOTA_CODE)
        limit = int(quota["Quota"]["Value"])
    except ClientError as err:
        print(f"quota check skipped ({err.response['Error']['Code']})")
        return
    if limit < needed:
        print(
            f"WARNING: G-instance vCPU quota is {limit}, but {instance_type} needs {needed}. "
            f"Request an increase for quota {GPU_QUOTA_CODE} or the launch will fail."
        )
    else:
        print(f"G-instance vCPU quota ok ({limit} >= {needed})")


def upload_bootstrap(rig: Rig) -> None:
    s3 = rig.session.client("s3")
    bucket = rig.cfg["s3"]["outputs_bucket"]
    for path in sorted((REPO_DIR / "bootstrap").iterdir()):
        if path.is_file():
            s3.upload_file(Filename=str(path), Bucket=bucket, Key=f"bootstrap/{path.name}")
    print(f"bootstrap files uploaded to s3://{bucket}/bootstrap/")


def store_hf_token(rig: Rig, token: str) -> None:
    ssm = rig.session.client("ssm")
    param = resource_names(rig.cfg)["hf_token_param"]
    if token:
        ssm.put_parameter(Name=param, Value=token, Type="SecureString", Overwrite=True)
        print(f"HF token stored in SSM parameter {param}")
        return
    try:
        ssm.get_parameter(Name=param)
        print(
            f"HF_TOKEN not set locally, but {param} exists from a previous launch and WILL be used "
            f"by the instance. To prevent that: aws ssm delete-parameter --name {param}"
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ParameterNotFound":
            raise
        print("HF_TOKEN not set locally: gated Hugging Face downloads will be skipped")


def build_user_data(cfg: dict) -> str:
    bucket = cfg["s3"]["outputs_bucket"]
    region = cfg["aws"]["region"]
    groups = ",".join(cfg["bootstrap"]["model_groups"])
    return f"""#!/bin/bash
set -euo pipefail
exec > /var/log/rig-userdata.log 2>&1
# Fail-safe: if anything below (S3 fetch, packages, bootstrap.sh) dies before
# the idle watchdog is active, the box still powers off (which stops it; EBS
# persists) instead of billing forever. bootstrap.sh cancels this once the
# watchdog runs.
shutdown -h +180
cat > /etc/rig.env <<EOF
RIG_OUTPUTS_BUCKET={bucket}
RIG_REGION={region}
RIG_MODEL_GROUPS={groups}
RIG_IDLE_STOP_MINUTES={cfg["rig"]["idle_stop_minutes"]}
RIG_MAX_JOB_MINUTES={cfg["rig"]["max_job_minutes"]}
RIG_COMFY_PORT={cfg["rig"]["comfyui_port"]}
RIG_HF_TOKEN_PARAM={resource_names(cfg)["hf_token_param"]}
EOF
mkdir -p /opt/rig-bootstrap
aws s3 cp s3://{bucket}/bootstrap/ /opt/rig-bootstrap/ --recursive --region {region}
bash /opt/rig-bootstrap/bootstrap.sh
"""


def run_instance_params(cfg: dict, ami_id: str, sg_id: str, subnet_id: str) -> dict:
    params = {
        "ImageId": ami_id,
        "InstanceType": cfg["rig"]["instance_type"],
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": subnet_id,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": {"Name": resource_names(cfg)["profile"]},
        "InstanceInitiatedShutdownBehavior": "stop",
        "UserData": build_user_data(cfg),
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": cfg["rig"]["root_volume_gb"],
                    "VolumeType": cfg["rig"]["root_volume_type"],
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tag_list(cfg, cfg["rig"]["name"])},
            {"ResourceType": "volume", "Tags": tag_list(cfg, cfg["rig"]["name"])},
        ],
    }
    if cfg["rig"]["key_name"]:
        params["KeyName"] = cfg["rig"]["key_name"]
    return params


def run_instance(rig: Rig, ami_id: str, sg_id: str, sleep=time.sleep) -> str:
    ec2 = rig.session.client("ec2")
    _, subnet_id = resolve_vpc_and_subnet(ec2, rig.cfg)
    params = run_instance_params(rig.cfg, ami_id, sg_id, subnet_id)
    # A freshly created instance profile can take up to a minute to propagate;
    # run_instances then fails with InvalidParameterValue until it does.
    for attempt in range(6):
        try:
            resp = ec2.run_instances(**params)
            return resp["Instances"][0]["InstanceId"]
        except ClientError as err:
            code = err.response["Error"]["Code"]
            message = err.response["Error"].get("Message", "")
            if code == "InvalidParameterValue" and "profile" in message.lower() and attempt < 5:
                print("instance profile not propagated yet, retrying in 10s")
                sleep(10)
                continue
            raise
    raise RuntimeError("run_instances retries exhausted")


def cmd_launch(rig: Rig) -> None:
    existing = find_rig_instance(rig)
    if existing:
        raise SystemExit(f"instance {existing['InstanceId']} already exists: use status, start or terminate")
    check_gpu_quota(rig)
    ensure_bucket(rig)
    ensure_role(rig)
    sg_id = ensure_security_group(rig)
    upload_bootstrap(rig)
    store_hf_token(rig, os.environ.get("HF_TOKEN", ""))
    ami_id = resolve_ami(rig)
    print(f"launching {rig.cfg['rig']['instance_type']} from {ami_id}")
    instance_id = run_instance(rig, ami_id, sg_id)
    write_state(rig, {"instance_id": instance_id, "sg_id": sg_id, "ami_id": ami_id})
    rig.session.client("ec2").get_waiter("instance_running").wait(InstanceIds=[instance_id])
    print(f"instance running: {instance_id}")
    print("bootstrap (models download) takes 30 to 60 min. Follow along with:")
    print("  python3 launch_rig.py session   # then: tail -f /var/log/rig-bootstrap.log")
    print("then open the UI with:")
    print(f"  python3 launch_rig.py tunnel    # http://localhost:{rig.cfg['rig']['comfyui_port']}")


# --- lifecycle -----------------------------------------------------------


def instance_info(rig: Rig, instance_id: str) -> dict | None:
    try:
        resp = rig.session.client("ec2").describe_instances(InstanceIds=[instance_id])
    except ClientError:
        return None
    for reservation in resp["Reservations"]:
        for inst in reservation["Instances"]:
            if inst["State"]["Name"] != "terminated":
                return inst
    return None


def instance_tags(inst: dict) -> dict:
    return {t["Key"]: t["Value"] for t in inst.get("Tags", [])}


def rig_instance_filters(cfg: dict) -> list[dict]:
    filters = [
        {"Name": "tag:Name", "Values": [cfg["rig"]["name"]]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    return filters + [{"Name": f"tag:{key}", "Values": [value]} for key, value in cfg.get("tags", {}).items()]


def has_rig_tags(tags: dict, cfg: dict, name: str) -> bool:
    """True when tags carry the given Name and every configured tag, Owner included."""
    expected = {**cfg.get("tags", {}), "Name": name}
    return all(tags.get(key) == value for key, value in expected.items())


def find_rig_instance(rig: Rig) -> dict | None:
    """Locate the rig by state file, falling back to tags if state was lost.

    Both paths verify every rig tag so lifecycle commands (stop/terminate)
    can never act on an unrelated instance."""
    name = rig.cfg["rig"]["name"]
    state = read_state(rig)
    if state.get("instance_id"):
        info = instance_info(rig, state["instance_id"])
        if info and has_rig_tags(instance_tags(info), rig.cfg, name):
            return info
        if info:
            print(f"WARNING: ignoring {state['instance_id']} from .rig_state.json: it lacks the rig tags")
    resp = rig.session.client("ec2").describe_instances(Filters=rig_instance_filters(rig.cfg))
    matches = [inst for r in resp["Reservations"] for inst in r["Instances"]]
    if len(matches) > 1:
        ids = ", ".join(m["InstanceId"] for m in matches)
        raise SystemExit(f"multiple instances match the rig tags ({ids}); put the right instance_id in .rig_state.json")
    if matches:
        write_state(rig, {**state, "instance_id": matches[0]["InstanceId"]})
        return matches[0]
    return None


def require_instance(rig: Rig) -> dict:
    info = find_rig_instance(rig)
    if not info:
        raise SystemExit("no rig instance found: run python3 launch_rig.py launch")
    return info


def cmd_status(rig: Rig) -> None:
    info = require_instance(rig)
    print(f"instance:   {info['InstanceId']} ({info['InstanceType']})")
    print(f"state:      {info['State']['Name']}")
    print(f"launched:   {info['LaunchTime']}")
    print(f"private ip: {info.get('PrivateIpAddress', '-')}")
    print(f"public ip:  {info.get('PublicIpAddress', '-')}")


def cmd_stop(rig: Rig) -> None:
    info = require_instance(rig)
    rig.session.client("ec2").stop_instances(InstanceIds=[info["InstanceId"]])
    print(f"stopping {info['InstanceId']} (EBS and models persist, compute billing stops)")


def cmd_start(rig: Rig) -> None:
    info = require_instance(rig)
    ec2 = rig.session.client("ec2")
    ec2.start_instances(InstanceIds=[info["InstanceId"]])
    ec2.get_waiter("instance_running").wait(InstanceIds=[info["InstanceId"]])
    print(f"started {info['InstanceId']}")


def confirm_terminate(rig: Rig, instance_id: str) -> bool:
    if rig.dry_run:
        print("[dry-run] skipped the typed 'terminate' confirmation")
        return True
    answer = input(f"Terminate {instance_id} and DELETE its EBS volume (all models)? Type 'terminate' to confirm: ")
    return answer.strip() == "terminate"


def cmd_terminate(rig: Rig) -> None:
    info = require_instance(rig)
    if not confirm_terminate(rig, info["InstanceId"]):
        raise SystemExit("aborted")
    rig.session.client("ec2").terminate_instances(InstanceIds=[info["InstanceId"]])
    state = {k: v for k, v in read_state(rig).items() if k != "instance_id"}
    write_state(rig, state)  # keep sg_id so deprovision can finish the teardown
    print(f"terminating {info['InstanceId']}")


def ssm_session_command(cfg: dict, instance_id: str, port_forward: bool) -> list[str]:
    argv = ["aws", "ssm", "start-session", "--target", instance_id]
    if port_forward:
        port = str(cfg["rig"]["comfyui_port"])
        argv += [
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            f"portNumber={port},localPortNumber={port}",
        ]
    return [*argv, "--region", cfg["aws"]["region"]]


def cmd_tunnel(rig: Rig) -> None:
    info = require_instance(rig)
    port = rig.cfg["rig"]["comfyui_port"]
    print(f"forwarding http://localhost:{port} to {info['InstanceId']}:{port} (ctrl-c to close)")
    exec_command(rig, ssm_session_command(rig.cfg, info["InstanceId"], port_forward=True))


def cmd_session(rig: Rig) -> None:
    info = require_instance(rig)
    exec_command(rig, ssm_session_command(rig.cfg, info["InstanceId"], port_forward=False))


# --- deprovision ---------------------------------------------------------


def cleanup_step(label: str, call, **kwargs) -> bool:
    """Run one deprovision call; tolerate already-deleted resources so the
    command is safely re-runnable after a partial failure."""
    try:
        call(**kwargs)
        print(f"deleted {label}")
        return True
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in ("NoSuchEntity", "InvalidGroup.NotFound", "ParameterNotFound"):
            print(f"{label}: already gone")
            return True
        print(f"{label}: {code}: {err.response['Error'].get('Message', '')}")
        return False


def rig_security_group_filters(cfg: dict, vpc_id: str) -> list[dict]:
    """Filters matching only the rig's own group: its name and every rig tag, in the rig's VPC."""
    name = resource_names(cfg)["sg"]
    tags = {**cfg.get("tags", {}), "Name": name}
    return [
        {"Name": "group-name", "Values": [name]},
        {"Name": "vpc-id", "Values": [vpc_id]},
        *({"Name": f"tag:{key}", "Values": [value]} for key, value in tags.items()),
    ]


def find_rig_security_groups(rig: Rig, ec2) -> set[str]:
    """Groups in the rig's VPC carrying the rig's name and tags. The state-file
    sg_id is included only when it passes the same filters, so a stale state
    file cannot point deletion at an unrelated group."""
    vpc_id, _ = resolve_vpc_and_subnet(ec2, rig.cfg)
    filters = rig_security_group_filters(rig.cfg, vpc_id)
    resp = ec2.describe_security_groups(Filters=filters)
    sg_ids = {g["GroupId"] for g in resp["SecurityGroups"]}
    state_sg = read_state(rig).get("sg_id")
    if state_sg and state_sg not in sg_ids:
        try:
            owned = ec2.describe_security_groups(GroupIds=[state_sg], Filters=filters)["SecurityGroups"]
        except ClientError as err:
            if err.response["Error"]["Code"] != "InvalidGroup.NotFound":
                raise
            owned = []
        if owned:
            sg_ids.add(state_sg)
        else:
            print(f"WARNING: ignoring {state_sg} from .rig_state.json: not the rig's group in {vpc_id}")
    return sg_ids


def delete_security_group(ec2, sg_id: str, sleep=time.sleep) -> bool:
    """ENIs from a just-terminated instance release asynchronously, so retry
    DependencyViolation for a minute before giving up."""
    for attempt in range(4):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            print(f"deleted security group {sg_id}")
            return True
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if code == "InvalidGroup.NotFound":
                print(f"security group {sg_id}: already gone")
                return True
            if code == "DependencyViolation" and attempt < 3:
                print(f"security group {sg_id}: still in use, retrying in 15s")
                sleep(15)
                continue
            print(f"security group {sg_id}: {code}: {err.response['Error'].get('Message', '')}")
            return False
    return False


def cmd_deprovision(rig: Rig) -> None:
    if find_rig_instance(rig):
        raise SystemExit("terminate the instance first: python3 launch_rig.py terminate")
    names = resource_names(rig.cfg)
    iam = rig.session.client("iam")
    ec2 = rig.session.client("ec2")
    ssm = rig.session.client("ssm")
    steps = [
        ("role from instance profile", iam.remove_role_from_instance_profile,
         {"InstanceProfileName": names["profile"], "RoleName": names["role"]}),
        (f"instance profile {names['profile']}", iam.delete_instance_profile,
         {"InstanceProfileName": names["profile"]}),
        (f"inline policy {INLINE_POLICY_NAME}", iam.delete_role_policy,
         {"RoleName": names["role"], "PolicyName": INLINE_POLICY_NAME}),
        ("SSM managed policy attachment", iam.detach_role_policy,
         {"RoleName": names["role"], "PolicyArn": SSM_MANAGED_POLICY}),
        (f"role {names['role']}", iam.delete_role, {"RoleName": names["role"]}),
        (f"HF token parameter {names['hf_token_param']}", ssm.delete_parameter, {"Name": names["hf_token_param"]}),
    ]  # fmt: skip
    results = [cleanup_step(label, call, **kwargs) for label, call, kwargs in steps]
    results += [delete_security_group(ec2, sg_id) for sg_id in sorted(find_rig_security_groups(rig, ec2))]
    print(f"kept bucket s3://{rig.cfg['s3']['outputs_bucket']} (delete manually if done)")
    if not all(results):
        raise SystemExit("deprovision incomplete: re-run after resolving the errors above")


COMMANDS = {
    "provision": cmd_provision,
    "launch": cmd_launch,
    "status": cmd_status,
    "stop": cmd_stop,
    "start": cmd_start,
    "terminate": cmd_terminate,
    "tunnel": cmd_tunnel,
    "session": cmd_session,
    "deprovision": cmd_deprovision,
}
# Commands that act on an existing instance; a dry run of these assumes one exists.
LIFECYCLE_COMMANDS = {"status", "stop", "start", "terminate", "tunnel", "session"}


def build_rig(command: str, dry_run: bool) -> Rig:
    if not dry_run:
        cfg = load_config()
        return Rig(cfg, aws_session(cfg))
    cfg = load_config(require_bucket=False)
    instance_exists = command in LIFECYCLE_COMMANDS
    resources_exist = instance_exists or command == "deprovision"
    if instance_exists:
        scenario = "the rig is provisioned and its instance exists"
    elif resources_exist:
        scenario = "the rig is provisioned and its instance is terminated"
    else:
        scenario = "no rig resources exist yet"
    print(f"dry run: no AWS call is made. Placeholder responses assume {scenario}.")
    responses = placeholder_responses(cfg, resources_exist=resources_exist, instance_exists=instance_exists)
    return Rig(cfg, DryRunSession(responses, root=REPO_DIR), dry_run=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=COMMAND_HELP, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=COMMANDS, metavar="command")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print each AWS call and its arguments instead of making it; needs no credentials",
    )
    args = parser.parse_args(argv)
    COMMANDS[args.command](build_rig(args.command, args.dry_run))


if __name__ == "__main__":
    main()
