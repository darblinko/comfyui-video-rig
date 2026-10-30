"""Shared stubs. No test makes an AWS call or a network call. Only
test_media_ffmpeg.py runs ffmpeg, and it is skipped when ffmpeg is absent.

Every test runs with placeholder AWS credentials, no AWS config or credentials
file and EC2 instance metadata disabled, so a test that creates a real boto3
client never reads the developer's credentials or probes the metadata endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from rig_config import load_config

REPO_DIR = Path(__file__).resolve().parent.parent
TEST_BUCKET = "example-rig-bucket"


def client_error(code: str, message: str = "", operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class EntityAlreadyExistsError(ClientError):
    pass


class FakeExceptions:
    EntityAlreadyExistsException = EntityAlreadyExistsError


class FakeWaiter:
    def __init__(self, session: FakeSession, name: str):
        self.session = session
        self.name = name

    def wait(self, **kwargs) -> None:
        self.session.calls.append((f"waiter.{self.name}", kwargs))


class FakeClient:
    def __init__(self, service: str, session: FakeSession):
        self.service = service
        self.session = session
        self.exceptions = FakeExceptions()

    def get_waiter(self, name: str) -> FakeWaiter:
        return FakeWaiter(self.session, name)

    def __getattr__(self, operation: str):
        if operation.startswith("_"):
            raise AttributeError(operation)
        name = f"{self.service}.{operation}"

        def call(*args, **kwargs):
            positional = {f"arg{index}": value for index, value in enumerate(args)}
            self.session.calls.append((name, {**positional, **kwargs}))
            response = self.session.responses.get(name, {})
            if isinstance(response, list):
                response = response.pop(0)
            if callable(response):
                response = response(*args, **kwargs)
            if isinstance(response, Exception):
                raise response
            return response

        return call


class FakeSession:
    """Stands in for boto3.Session. responses maps "service.operation" to a
    dict, an exception, a callable, or a list consumed one item per call."""

    def __init__(self, responses: dict | None = None):
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, object]] = []

    def client(self, service: str, **_kwargs) -> FakeClient:
        return FakeClient(service, self)

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_of(self, operation: str) -> list[dict]:
        """Arguments of each call to operation; positional ones are keyed arg0, arg1 and so on."""
        return [kwargs for name, kwargs in self.calls if name == operation]


AWS_ENV_TO_CLEAR = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE")
AWS_ENV_TO_CLEAR += ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI")


@pytest.fixture(autouse=True)
def isolated_aws_env(monkeypatch, tmp_path):
    for name in AWS_ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))


@pytest.fixture
def cfg() -> dict:
    return load_config(env={"RIG_OUTPUTS_BUCKET": TEST_BUCKET, "AWS_REGION": "us-west-2"})
