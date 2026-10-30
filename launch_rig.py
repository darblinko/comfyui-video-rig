#!/usr/bin/env python3
"""Launch and manage the ComfyUI video GPU rig.

All knobs live in rig.config.toml; account-specific values come from the
environment (AWS_PROFILE, AWS_REGION, RIG_OUTPUTS_BUCKET, RIG_OWNER_TAG).
Subcommands:

  provision    Create the S3 outputs bucket, IAM role/instance profile, security group
  launch       Launch the GPU instance (uploads bootstrap/ to S3 first)
  status       Show instance state, IPs, launch time
  stop         Stop the instance (compute billing stops, EBS + models persist)
  start        Start a stopped instance
  terminate    Terminate the instance and its EBS volume (asks for confirmation)
  tunnel       SSM port-forward to ComfyUI (http://localhost:<port>)
  session      Interactive SSM shell on the instance
  deprovision  Delete the SG and IAM role/profile (bucket is kept)

No inbound ports are ever opened; all access goes through SSM.
If the HF_TOKEN env var is set at launch, it is stored as an SSM SecureString
(/comfyui-video-rig/hf-token) that the instance reads for gated Hugging Face downloads.
"""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

PROTO_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROTO_DIR / "rig.config.toml"
STATE_FILE = PROTO_DIR / ".rig_state.json"
HF_TOKEN_PARAM = "/comfyui-video-rig/hf-token"
GPU_QUOTA_CODE = "L-DB2E81BA"  # Running On-Demand G and VT instances (vCPUs)

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


def load_config() -> dict:
    with open(CONFIG_FILE, "rb") as fh:
        cfg = tomllib.load(fh)
    return apply_env_overrides(cfg, os.environ)


def apply_env_overrides(cfg: dict, env) -> dict:
    """Return a copy of cfg with account-specific values taken from the environment."""
    aws = {**cfg["aws"], "region": env.get("AWS_REGION") or cfg["aws"]["region"]}
    s3 = {**cfg["s3"], "outputs_bucket": env.get("RIG_OUTPUTS_BUCKET") or cfg["s3"]["outputs_bucket"]}
    tags = dict(cfg.get("tags", {}))
    if env.get("RIG_OWNER_TAG"):
        tags["Owner"] = env["RIG_OWNER_TAG"]
    if not s3["outputs_bucket"]:
        raise SystemExit("set RIG_OUTPUTS_BUCKET (or [s3] outputs_bucket in rig.config.toml)")
    return {**cfg, "aws": aws, "s3": s3, "tags": tags}


def resource_names(cfg: dict) -> dict:
    name = cfg["rig"]["name"]
    return {
        "role": f"{name}-role",
        "profile": f"{name}-profile",
        "sg": f"{name}-sg",
    }


def aws_session(cfg: dict) -> boto3.Session:
    # profile_name is left unset so boto3 honours AWS_PROFILE or the default chain.
    return boto3.Session(region_name=cfg["aws"]["region"])


def read_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def tag_list(cfg: dict, name: str) -> list[dict]:
    tags = [{"Key": k, "Value": v} for k, v in cfg.get("tags", {}).items()]
    tags.append({"Key": "Name", "Value": name})
    return tags


# --- provision -----------------------------------------------------------


def ensure_bucket(session: boto3.Session, cfg: dict) -> None:
    s3 = session.client("s3")
    bucket = cfg["s3"]["outputs_bucket"]
    region = cfg["aws"]["region"]
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket exists: s3://{bucket}")
        return
    except ClientError as err:
        if err.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    print(f"created bucket: s3://{bucket}")


def instance_policy(cfg: dict, account_id: str) -> dict:
    bucket = cfg["s3"]["outputs_bucket"]
    region = cfg["aws"]["region"]
    # The instance only ever reads its bootstrap files; run outputs are synced
    # from the workstation. No PutObject: a compromised box must not be able to
    # overwrite bootstrap/bootstrap.sh (which the next launch runs as root).
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
                "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/comfyui-video-rig/*",
            },
        ],
    }


def ensure_role(session: boto3.Session, cfg: dict) -> str:
    iam = session.client("iam")
    names = resource_names(cfg)
    account_id = session.client("sts").get_caller_identity()["Account"]
    try:
        iam.create_role(
            RoleName=names["role"],
            AssumeRolePolicyDocument=json.dumps(EC2_TRUST_POLICY),
            Description="ComfyUI video rig instance role",
        )
        print(f"created role: {names['role']}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"role exists: {names['role']}")
    iam.attach_role_policy(
        RoleName=names["role"],
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )
    iam.put_role_policy(
        RoleName=names["role"],
        PolicyName="rig-instance-access",
        PolicyDocument=json.dumps(instance_policy(cfg, account_id)),
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


def ensure_security_group(session: boto3.Session, cfg: dict) -> str:
    ec2 = session.client("ec2")
    names = resource_names(cfg)
    vpc_id, _ = resolve_vpc_and_subnet(ec2, cfg)
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [names["sg"]]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing:
        print(f"security group exists: {existing[0]['GroupId']}")
        return existing[0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=names["sg"],
        Description="ComfyUI video rig - no inbound, SSM access only",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list(cfg, names["sg"])}],
    )
    print(f"created security group (no inbound rules): {sg['GroupId']}")
    return sg["GroupId"]


def cmd_provision(cfg: dict) -> None:
    session = aws_session(cfg)
    ensure_bucket(session, cfg)
    ensure_role(session, cfg)
    ensure_security_group(session, cfg)
    print("provision complete. Next: python3 launch_rig.py launch")


# --- launch --------------------------------------------------------------


def resolve_ami(session: boto3.Session, cfg: dict) -> str:
    if cfg["rig"]["ami_id"]:
        return cfg["rig"]["ami_id"]
    ssm = session.client("ssm")
    param = ssm.get_parameter(Name=cfg["rig"]["ami_ssm_parameter"])
    return param["Parameter"]["Value"]


def check_gpu_quota(session: boto3.Session, cfg: dict) -> None:
    try:
        ec2 = session.client("ec2")
        itype = ec2.describe_instance_types(InstanceTypes=[cfg["rig"]["instance_type"]])["InstanceTypes"][0]
        needed = itype["VCpuInfo"]["DefaultVCpus"]
        quotas = session.client("service-quotas")
        quota = quotas.get_service_quota(ServiceCode="ec2", QuotaCode=GPU_QUOTA_CODE)
        limit = int(quota["Quota"]["Value"])
        if limit < needed:
            print(
                f"WARNING: G-instance vCPU quota is {limit}, but "
                f"{cfg['rig']['instance_type']} needs {needed}. Request an increase "
                f"for quota {GPU_QUOTA_CODE} or the launch will fail."
            )
        else:
            print(f"G-instance vCPU quota ok ({limit} >= {needed})")
    except ClientError as err:
        print(f"quota check skipped ({err.response['Error']['Code']})")


def upload_bootstrap(session: boto3.Session, cfg: dict) -> None:
    s3 = session.client("s3")
    bucket = cfg["s3"]["outputs_bucket"]
    for path in sorted((PROTO_DIR / "bootstrap").iterdir()):
        if path.is_file():
            s3.upload_file(str(path), bucket, f"bootstrap/{path.name}")
    print(f"bootstrap files uploaded to s3://{bucket}/bootstrap/")


def store_hf_token(session: boto3.Session) -> None:
    ssm = session.client("ssm")
    token = os.environ.get("HF_TOKEN", "")
    if token:
        ssm.put_parameter(Name=HF_TOKEN_PARAM, Value=token, Type="SecureString", Overwrite=True)
        print(f"HF token stored in SSM parameter {HF_TOKEN_PARAM}")
        return
    try:
        ssm.get_parameter(Name=HF_TOKEN_PARAM)
        print(
            f"HF_TOKEN not set locally, but {HF_TOKEN_PARAM} exists from a previous "
            "launch and WILL be used by the instance. To prevent that: "
            f"aws ssm delete-parameter --name {HF_TOKEN_PARAM}"
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ParameterNotFound":
            raise
        print("HF_TOKEN not set locally - gated Hugging Face downloads will be skipped")


def build_user_data(cfg: dict) -> str:
    bucket = cfg["s3"]["outputs_bucket"]
    region = cfg["aws"]["region"]
    groups = ",".join(cfg["bootstrap"]["model_groups"])
    return f"""#!/bin/bash
set -euo pipefail
exec > /var/log/rig-userdata.log 2>&1
# Fail-safe: if anything below (S3 fetch, packages, bootstrap.sh) dies before
# the idle watchdog is active, the box still powers off (= stops, EBS persists)
# instead of billing forever. bootstrap.sh cancels this once the watchdog runs.
shutdown -h +180
cat > /etc/rig.env <<EOF
RIG_OUTPUTS_BUCKET={bucket}
RIG_REGION={region}
RIG_MODEL_GROUPS={groups}
RIG_IDLE_STOP_MINUTES={cfg["rig"]["idle_stop_minutes"]}
RIG_MAX_JOB_MINUTES={cfg["rig"]["max_job_minutes"]}
RIG_COMFY_PORT={cfg["rig"]["comfyui_port"]}
EOF
mkdir -p /opt/rig-bootstrap
aws s3 cp s3://{bucket}/bootstrap/ /opt/rig-bootstrap/ --recursive --region {region}
bash /opt/rig-bootstrap/bootstrap.sh
"""


def run_instance(session: boto3.Session, cfg: dict, ami_id: str, sg_id: str) -> str:
    ec2 = session.client("ec2")
    _, subnet_id = resolve_vpc_and_subnet(ec2, cfg)
    names = resource_names(cfg)
    params = {
        "ImageId": ami_id,
        "InstanceType": cfg["rig"]["instance_type"],
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": subnet_id,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": {"Name": names["profile"]},
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
                print("instance profile not propagated yet - retrying in 10s ...")
                time.sleep(10)
                continue
            raise
    raise RuntimeError("run_instances retries exhausted")


def cmd_launch(cfg: dict) -> None:
    existing = find_rig_instance(cfg)
    if existing:
        raise SystemExit(f"instance {existing['InstanceId']} already exists - use status/start/terminate")
    session = aws_session(cfg)
    check_gpu_quota(session, cfg)
    ensure_bucket(session, cfg)
    ensure_role(session, cfg)
    sg_id = ensure_security_group(session, cfg)
    upload_bootstrap(session, cfg)
    store_hf_token(session)
    ami_id = resolve_ami(session, cfg)
    print(f"launching {cfg['rig']['instance_type']} from {ami_id} ...")
    instance_id = run_instance(session, cfg, ami_id, sg_id)
    write_state({"instance_id": instance_id, "sg_id": sg_id, "ami_id": ami_id})
    session.client("ec2").get_waiter("instance_running").wait(InstanceIds=[instance_id])
    print(f"instance running: {instance_id}")
    print("bootstrap (models download) takes 30-60 min. Follow along with:")
    print("  python3 launch_rig.py session   # then: tail -f /var/log/rig-bootstrap.log")
    print("then open the UI with:")
    print(f"  python3 launch_rig.py tunnel    # http://localhost:{cfg['rig']['comfyui_port']}")


# --- lifecycle -----------------------------------------------------------


def instance_info(cfg: dict, instance_id: str) -> dict | None:
    ec2 = aws_session(cfg).client("ec2")
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError:
        return None
    for reservation in resp["Reservations"]:
        for inst in reservation["Instances"]:
            if inst["State"]["Name"] != "terminated":
                return inst
    return None


def instance_tags(inst: dict) -> dict:
    return {t["Key"]: t["Value"] for t in inst.get("Tags", [])}


def find_rig_instance(cfg: dict) -> dict | None:
    """Locate the rig by state file, falling back to tags if state was lost.

    Both paths verify the rig tags so lifecycle commands (stop/terminate)
    can never act on an unrelated instance."""
    name = cfg["rig"]["name"]
    state = read_state()
    if state.get("instance_id"):
        info = instance_info(cfg, state["instance_id"])
        if info and instance_tags(info).get("Name") == name:
            return info
        if info:
            print(f"WARNING: ignoring {state['instance_id']} from .rig_state.json - its Name tag is not '{name}'")
    filters = [
        {"Name": "tag:Name", "Values": [name]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped"],
        },
    ]
    for key, value in cfg.get("tags", {}).items():
        filters.append({"Name": f"tag:{key}", "Values": [value]})
    resp = aws_session(cfg).client("ec2").describe_instances(Filters=filters)
    matches = [inst for r in resp["Reservations"] for inst in r["Instances"]]
    if len(matches) > 1:
        ids = ", ".join(m["InstanceId"] for m in matches)
        raise SystemExit(
            f"multiple instances match the rig tags ({ids}) - put the right instance_id in .rig_state.json"
        )
    if matches:
        write_state({**state, "instance_id": matches[0]["InstanceId"]})
        return matches[0]
    return None


def require_instance(cfg: dict) -> dict:
    info = find_rig_instance(cfg)
    if not info:
        raise SystemExit("no rig instance found - run: python3 launch_rig.py launch")
    return info


def cmd_status(cfg: dict) -> None:
    info = require_instance(cfg)
    print(f"instance:   {info['InstanceId']} ({info['InstanceType']})")
    print(f"state:      {info['State']['Name']}")
    print(f"launched:   {info['LaunchTime']}")
    print(f"private ip: {info.get('PrivateIpAddress', '-')}")
    print(f"public ip:  {info.get('PublicIpAddress', '-')}")


def cmd_stop(cfg: dict) -> None:
    info = require_instance(cfg)
    aws_session(cfg).client("ec2").stop_instances(InstanceIds=[info["InstanceId"]])
    print(f"stopping {info['InstanceId']} (EBS + models persist, compute billing stops)")


def cmd_start(cfg: dict) -> None:
    info = require_instance(cfg)
    ec2 = aws_session(cfg).client("ec2")
    ec2.start_instances(InstanceIds=[info["InstanceId"]])
    ec2.get_waiter("instance_running").wait(InstanceIds=[info["InstanceId"]])
    print(f"started {info['InstanceId']}")


def cmd_terminate(cfg: dict) -> None:
    info = require_instance(cfg)
    answer = input(
        f"Terminate {info['InstanceId']} and DELETE its EBS volume (all models)? Type 'terminate' to confirm: "
    )
    if answer.strip() != "terminate":
        raise SystemExit("aborted")
    aws_session(cfg).client("ec2").terminate_instances(InstanceIds=[info["InstanceId"]])
    state = read_state()
    state.pop("instance_id", None)
    write_state(state)  # keep sg_id etc. so deprovision can finish the teardown
    print(f"terminating {info['InstanceId']}")


def cmd_tunnel(cfg: dict) -> None:
    info = require_instance(cfg)
    port = str(cfg["rig"]["comfyui_port"])
    print(f"forwarding http://localhost:{port} -> {info['InstanceId']}:{port} (ctrl-c to close)")
    os.execvp(
        "aws",
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            info["InstanceId"],
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            f"portNumber={port},localPortNumber={port}",
            "--region",
            cfg["aws"]["region"],
        ],
    )


def cmd_session(cfg: dict) -> None:
    info = require_instance(cfg)
    os.execvp(
        "aws",
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            info["InstanceId"],
            "--region",
            cfg["aws"]["region"],
        ],
    )


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
        print(f"{label}: {code} - {err.response['Error'].get('Message', '')}")
        return False


def find_rig_security_groups(ec2, cfg: dict) -> set[str]:
    """Union of the state-file sg_id and any group matching the rig's name, so
    a lost state file cannot orphan the SG."""
    sg_ids = set()
    if read_state().get("sg_id"):
        sg_ids.add(read_state()["sg_id"])
    resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [resource_names(cfg)["sg"]]}])
    sg_ids.update(g["GroupId"] for g in resp["SecurityGroups"])
    return sg_ids


def delete_security_group(ec2, sg_id: str) -> bool:
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
                time.sleep(15)
                continue
            print(f"security group {sg_id}: {code} - {err.response['Error'].get('Message', '')}")
            return False
    return False


def cmd_deprovision(cfg: dict) -> None:
    if find_rig_instance(cfg):
        raise SystemExit("terminate the instance first: python3 launch_rig.py terminate")
    session = aws_session(cfg)
    names = resource_names(cfg)
    iam = session.client("iam")
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    results = [
        cleanup_step(
            "role from instance profile",
            iam.remove_role_from_instance_profile,
            InstanceProfileName=names["profile"],
            RoleName=names["role"],
        ),
        cleanup_step(
            f"instance profile {names['profile']}",
            iam.delete_instance_profile,
            InstanceProfileName=names["profile"],
        ),
        cleanup_step(
            "inline policy rig-instance-access",
            iam.delete_role_policy,
            RoleName=names["role"],
            PolicyName="rig-instance-access",
        ),
        cleanup_step(
            "SSM managed policy attachment",
            iam.detach_role_policy,
            RoleName=names["role"],
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        ),
        cleanup_step(f"role {names['role']}", iam.delete_role, RoleName=names["role"]),
        cleanup_step(
            f"HF token parameter {HF_TOKEN_PARAM}",
            ssm.delete_parameter,
            Name=HF_TOKEN_PARAM,
        ),
    ]
    results += [delete_security_group(ec2, sg_id) for sg_id in find_rig_security_groups(ec2, cfg)]
    print(f"kept bucket s3://{cfg['s3']['outputs_bucket']} (delete manually if done)")
    if not all(results):
        raise SystemExit("deprovision incomplete - re-run after resolving the errors above")


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


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        raise SystemExit(f"usage: python3 launch_rig.py <{'|'.join(COMMANDS)}>")
    COMMANDS[sys.argv[1]](load_config())


if __name__ == "__main__":
    main()
