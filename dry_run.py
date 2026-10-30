"""A stand-in for boto3.Session that prints each AWS call instead of making it.

launch_rig.py --dry-run passes a DryRunSession to the same command functions
that normally receive a boto3 session. Every client method call is printed with
its arguments; the return value comes from a table of placeholder responses so
the command can run to the end without credentials or network access.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from botocore.exceptions import ClientError

REDACTED = "<redacted>"
INDENT = "    "
INLINE_JSON_LIMIT = 100


def placeholder_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "dry-run placeholder"}}, operation)


class _ModeledExceptions:
    """Mimics client.exceptions: any attribute is a ClientError subclass, so
    `except client.exceptions.EntityAlreadyExistsException` stays valid."""

    def __getattr__(self, name: str) -> type[ClientError]:
        return type(name, (ClientError,), {})


class DryRunWaiter:
    def __init__(self, service: str, name: str, out: Callable[[str], None]):
        self.service = service
        self.name = name
        self.out = out

    def wait(self, **kwargs) -> None:
        self.out(f"[dry-run] {self.service} waiter {self.name}: {json.dumps(kwargs)}")


class DryRunClient:
    def __init__(self, service: str, session: DryRunSession):
        self.service = service
        self.session = session
        self.exceptions = _ModeledExceptions()

    def get_waiter(self, name: str) -> DryRunWaiter:
        return DryRunWaiter(self.service, name, self.session.out)

    def __getattr__(self, operation: str) -> Callable:
        if operation.startswith("_"):
            raise AttributeError(operation)

        def call(*args, **kwargs):
            self.session.out(format_call(self.service, operation, args, kwargs, self.session.root))
            response = self.session.responses.get(f"{self.service}.{operation}", {})
            if callable(response):
                response = response(**kwargs)
            if isinstance(response, Exception):
                raise response
            return response

        return call


class DryRunSession:
    """responses maps "service.operation" to a dict, an exception to raise, or a
    callable taking the call's kwargs and returning either."""

    def __init__(self, responses: dict, root: Path | None = None, out: Callable[[str], None] = print):
        self.responses = responses
        self.root = root
        self.out = out

    def client(self, service: str, **_kwargs) -> DryRunClient:
        return DryRunClient(service, self)


def format_call(service: str, operation: str, args: tuple, kwargs: dict, root: Path | None = None) -> str:
    lines = [f"[dry-run] {service}.{operation}"]
    for position, value in enumerate(args):
        lines += format_field(f"arg{position}", display_value(value, root))
    for key, value in kwargs.items():
        if operation == "put_parameter" and key == "Value":
            value = REDACTED
        lines += format_field(key, display_value(value, root))
    return "\n".join(lines)


def display_value(value, root: Path | None):
    """Show local paths relative to the repo so output carries no home directory."""
    if root is not None and isinstance(value, str) and value.startswith(f"{root}/"):
        return value[len(str(root)) + 1 :]
    return value


def format_field(key: str, value) -> list[str]:
    if isinstance(value, str):
        parsed = parse_json_object(value)
        if parsed is not None:
            return format_structure(f"{key} (JSON string)", parsed)
        if "\n" in value:
            return [f"{INDENT}{key}: |", *indent_block(value.rstrip("\n"))]
        return [f"{INDENT}{key}: {value}"]
    if isinstance(value, (dict, list)):
        return format_structure(key, value)
    return [f"{INDENT}{key}: {value}"]


def format_structure(key: str, value: dict | list) -> list[str]:
    compact = json.dumps(value, default=str)
    if len(compact) <= INLINE_JSON_LIMIT:
        return [f"{INDENT}{key}: {compact}"]
    return [f"{INDENT}{key}:", *indent_block(json.dumps(value, indent=2, default=str))]


def indent_block(text: str) -> list[str]:
    return [f"{INDENT * 2}{line}" for line in text.splitlines()]


def parse_json_object(text: str) -> dict | None:
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def placeholder_instance(cfg: dict) -> dict:
    tags = [{"Key": k, "Value": v} for k, v in cfg.get("tags", {}).items()]
    return {
        "InstanceId": "i-<rig>",
        "InstanceType": cfg["rig"]["instance_type"],
        "State": {"Name": "running"},
        "LaunchTime": "<launch time>",
        "PrivateIpAddress": "<private ip>",
        "Tags": [*tags, {"Key": "Name", "Value": cfg["rig"]["name"]}],
    }


def placeholder_responses(cfg: dict, *, resources_exist: bool, instance_exists: bool) -> dict:
    """Responses for an account where the rig's bucket, role and SG either all
    exist or none do, with or without the rig instance."""
    region = cfg["aws"]["region"]
    ami_parameter = cfg["rig"]["ami_ssm_parameter"]
    role = f"{cfg['rig']['name']}-role"

    def get_parameter(**kwargs):
        if kwargs.get("Name") == ami_parameter:
            return {"Parameter": {"Value": "ami-<latest>"}}
        return placeholder_error("ParameterNotFound", "GetParameter")

    return {
        "s3.head_bucket": {} if resources_exist else placeholder_error("404", "HeadBucket"),
        "sts.get_caller_identity": {"Account": "<ACCOUNT_ID>"},
        "iam.get_instance_profile": {"InstanceProfile": {"Roles": [{"RoleName": role}] if resources_exist else []}},
        "ec2.describe_vpcs": {"Vpcs": [{"VpcId": "vpc-<default>"}]},
        "ec2.describe_subnets": {
            "Subnets": [{"SubnetId": "subnet-<first>", "VpcId": "vpc-<default>", "AvailabilityZone": f"{region}a"}]
        },
        "ec2.describe_security_groups": {"SecurityGroups": [{"GroupId": "sg-<rig>"}] if resources_exist else []},
        "ec2.create_security_group": {"GroupId": "sg-<new>"},
        "ec2.describe_instance_types": {"InstanceTypes": [{"VCpuInfo": {"DefaultVCpus": 0}}]},
        "service-quotas.get_service_quota": placeholder_error("DryRunNoQuotaLookup", "GetServiceQuota"),
        "ssm.get_parameter": get_parameter,
        "ec2.run_instances": {"Instances": [{"InstanceId": "i-<new>"}]},
        "ec2.describe_instances": {
            "Reservations": [{"Instances": [placeholder_instance(cfg)]}] if instance_exists else []
        },
    }
