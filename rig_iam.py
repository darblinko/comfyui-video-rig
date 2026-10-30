"""The rig's IAM role, instance profile and Hugging Face token parameter.

Their names derive from rig.name, so two deployments in one account that keep
the default name would otherwise share them. Each one is created with the rig
tags plus RigBucket (the outputs bucket, which is unique per deployment), and an
existing one is changed or deleted only when it carries the same tags. The role
must also trust only EC2, and the instance profile must hold no other role.
"""

from __future__ import annotations

import json
import urllib.parse

from botocore.exceptions import ClientError

SSM_MANAGED_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
INLINE_POLICY_NAME = "rig-instance-access"
OWNERSHIP_TAG = "RigBucket"
EC2_SERVICE = "ec2.amazonaws.com"
# AmazonSSMManagedInstanceCore allows ssm:GetParameter and ssm:GetParameters on
# every resource; the inline policy denies these reads for any other parameter.
PARAMETER_READ_ACTIONS = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:GetParameterHistory",
]

EC2_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": EC2_SERVICE},
            "Action": "sts:AssumeRole",
        }
    ],
}


def resource_names(cfg: dict) -> dict:
    name = cfg["rig"]["name"]
    return {
        "role": f"{name}-role",
        "profile": f"{name}-profile",
        "sg": f"{name}-sg",
        "hf_token_param": f"/{name}/hf-token",
    }


def ownership_tags(cfg: dict, name: str) -> dict:
    return {**cfg.get("tags", {}), "Name": name, OWNERSHIP_TAG: cfg["s3"]["outputs_bucket"]}


def tag_pairs(tags: dict) -> list[dict]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def tags_match(actual: list[dict], expected: dict) -> bool:
    found = {tag["Key"]: tag["Value"] for tag in actual}
    return all(found.get(key) == value for key, value in expected.items())


def trusts_only_ec2(document) -> bool:
    if isinstance(document, str):
        document = json.loads(urllib.parse.unquote(document))
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    return bool(statements) and all(
        statement.get("Effect") == "Allow"
        and statement.get("Principal") in ({"Service": EC2_SERVICE}, {"Service": [EC2_SERVICE]})
        and statement.get("Action") in ("sts:AssumeRole", ["sts:AssumeRole"])
        and "Condition" not in statement
        for statement in statements
    )


def token_parameter_arn(cfg: dict, account_id: str) -> str:
    return f"arn:aws:ssm:{cfg['aws']['region']}:{account_id}:parameter{resource_names(cfg)['hf_token_param']}"


def instance_policy(cfg: dict, account_id: str) -> dict:
    bucket = cfg["s3"]["outputs_bucket"]
    token_arn = token_parameter_arn(cfg, account_id)
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
                "Resource": token_arn,
            },
            {
                "Sid": "NoOtherParameters",
                "Effect": "Deny",
                "Action": PARAMETER_READ_ACTIONS,
                "NotResource": token_arn,
            },
        ],
    }


def is_missing(err: ClientError) -> bool:
    return err.response["Error"]["Code"] in ("NoSuchEntity", "InvalidResourceId", "ParameterNotFound")


def role_state(iam, cfg: dict) -> tuple[bool, str | None]:
    """(exists, reason it is not the rig's role or None)."""
    name = resource_names(cfg)["role"]
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except ClientError as err:
        if is_missing(err):
            return False, None
        raise
    if not tags_match(role.get("Tags", []), ownership_tags(cfg, name)):
        return True, f"role {name} lacks the rig tags ({OWNERSHIP_TAG}={cfg['s3']['outputs_bucket']} and the rest)"
    if not trusts_only_ec2(role.get("AssumeRolePolicyDocument", {})):
        return True, f"role {name} trusts a principal other than EC2"
    return True, None


def profile_state(iam, cfg: dict) -> tuple[bool, str | None, list[str]]:
    """(exists, reason it is not the rig's profile or None, role names it holds)."""
    names = resource_names(cfg)
    try:
        profile = iam.get_instance_profile(InstanceProfileName=names["profile"])["InstanceProfile"]
    except ClientError as err:
        if is_missing(err):
            return False, None, []
        raise
    roles = [role["RoleName"] for role in profile.get("Roles", [])]
    if not tags_match(profile.get("Tags", []), ownership_tags(cfg, names["profile"])):
        return True, f"instance profile {names['profile']} lacks the rig tags", roles
    others = [role for role in roles if role != names["role"]]
    if others:
        return True, f"instance profile {names['profile']} holds role {', '.join(others)}", roles
    return True, None, roles


def parameter_state(ssm, cfg: dict) -> tuple[bool, str | None]:
    """(exists, reason it is not the rig's token parameter or None)."""
    name = resource_names(cfg)["hf_token_param"]
    try:
        tags = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name).get("TagList", [])
    except ClientError as err:
        if is_missing(err):
            return False, None
        raise
    if not tags_match(tags, ownership_tags(cfg, name)):
        return True, f"parameter {name} lacks the rig tags"
    return True, None


def refuse(problem: str) -> SystemExit:
    return SystemExit(
        f"{problem}: refusing to change it. It may belong to another deployment with the same rig.name; "
        f"set a different rig.name in rig.config.toml."
    )


def ensure_instance_role(iam, cfg: dict, account_id: str) -> str:
    """Create or verify the role and instance profile, then set the role's policies."""
    names = resource_names(cfg)
    exists, problem = role_state(iam, cfg)
    if problem:
        raise refuse(problem)
    if exists:
        print(f"role exists (rig tags and EC2 trust verified): {names['role']}")
    else:
        iam.create_role(
            RoleName=names["role"],
            AssumeRolePolicyDocument=json.dumps(EC2_TRUST_POLICY),
            Description="ComfyUI video rig instance role",
            Tags=tag_pairs(ownership_tags(cfg, names["role"])),
        )
        print(f"created role: {names['role']}")
    iam.attach_role_policy(RoleName=names["role"], PolicyArn=SSM_MANAGED_POLICY)
    iam.put_role_policy(
        RoleName=names["role"],
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(instance_policy(cfg, account_id)),
    )
    exists, problem, roles = profile_state(iam, cfg)
    if problem:
        raise refuse(problem)
    if exists:
        print(f"instance profile exists (rig tags verified): {names['profile']}")
    else:
        iam.create_instance_profile(
            InstanceProfileName=names["profile"], Tags=tag_pairs(ownership_tags(cfg, names["profile"]))
        )
        print(f"created instance profile: {names['profile']}")
    if not roles:
        iam.add_role_to_instance_profile(InstanceProfileName=names["profile"], RoleName=names["role"])
    return names["profile"]


def cleanup_plan(iam, ssm, cfg: dict) -> tuple[list[tuple], list[str]]:
    """Deprovision steps (label, call, kwargs) for the resources that are the
    rig's, and the reasons for each existing resource that is left alone."""
    names = resource_names(cfg)
    steps, refusals = [], []
    profile_exists, problem, roles = profile_state(iam, cfg)
    if problem:
        refusals.append(problem)
    elif profile_exists:
        if names["role"] in roles:
            steps.append(("role from instance profile", iam.remove_role_from_instance_profile,
                          {"InstanceProfileName": names["profile"], "RoleName": names["role"]}))  # fmt: skip
        steps.append((f"instance profile {names['profile']}", iam.delete_instance_profile,
                      {"InstanceProfileName": names["profile"]}))  # fmt: skip
    role_exists, problem = role_state(iam, cfg)
    if problem:
        refusals.append(problem)
    elif role_exists:
        steps += [
            (f"inline policy {INLINE_POLICY_NAME}", iam.delete_role_policy,
             {"RoleName": names["role"], "PolicyName": INLINE_POLICY_NAME}),
            ("SSM managed policy attachment", iam.detach_role_policy,
             {"RoleName": names["role"], "PolicyArn": SSM_MANAGED_POLICY}),
            (f"role {names['role']}", iam.delete_role, {"RoleName": names["role"]}),
        ]  # fmt: skip
    param_exists, problem = parameter_state(ssm, cfg)
    if problem:
        refusals.append(problem)
    elif param_exists:
        steps.append((f"HF token parameter {names['hf_token_param']}", ssm.delete_parameter,
                      {"Name": names["hf_token_param"]}))  # fmt: skip
    return steps, refusals
