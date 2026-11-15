import boto3
import pytest

from app.config import Settings
from app.deps import build_llm
from app.llm.base import LLMError
from app.llm.bedrock import BedrockLLM, make_bedrock_client


def test_explicit_keys_are_passed_to_session(monkeypatch):
    recorded = []

    class FakeSession:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

        def client(self, service_name, **kwargs):
            return f"fake_client_{service_name}"

    monkeypatch.setattr(boto3, "Session", FakeSession)

    client = make_bedrock_client(
        region="ap-southeast-1",
        access_key_id="test_key",
        secret_access_key="test_secret",
        session_token="test_token",
    )
    assert client == "fake_client_bedrock-runtime"
    assert len(recorded) == 1
    assert recorded[0] == {
        "aws_access_key_id": "test_key",
        "aws_secret_access_key": "test_secret",
        "aws_session_token": "test_token",
        "region_name": "ap-southeast-1",
    }

    # When session_token is empty string, aws_session_token is None
    recorded.clear()
    make_bedrock_client(
        region="ap-southeast-1",
        access_key_id="test_key",
        secret_access_key="test_secret",
    )
    assert len(recorded) == 1
    assert recorded[0] == {
        "aws_access_key_id": "test_key",
        "aws_secret_access_key": "test_secret",
        "aws_session_token": None,
        "region_name": "ap-southeast-1",
    }


def test_only_one_key_raises_llm_error():
    with pytest.raises(LLMError) as exc_info:
        make_bedrock_client(
            region="ap-southeast-1",
            access_key_id="only_key",
            secret_access_key="",
        )
    assert (
        "Set both GROUNDLINE_AWS_ACCESS_KEY_ID and GROUNDLINE_AWS_SECRET_ACCESS_KEY, or neither"
        in str(exc_info.value)
    )

    with pytest.raises(LLMError) as exc_info:
        make_bedrock_client(
            region="ap-southeast-1",
            access_key_id="",
            secret_access_key="only_secret",
        )
    assert (
        "Set both GROUNDLINE_AWS_ACCESS_KEY_ID and GROUNDLINE_AWS_SECRET_ACCESS_KEY, or neither"
        in str(exc_info.value)
    )


def test_profile_is_used_when_no_keys(monkeypatch):
    recorded = []

    class FakeSession:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

        def client(self, service_name, **kwargs):
            return f"fake_client_{service_name}"

    monkeypatch.setattr(boto3, "Session", FakeSession)

    client = make_bedrock_client(
        region="ap-southeast-1",
        profile="my_profile",
    )
    assert client == "fake_client_bedrock-runtime"
    assert len(recorded) == 1
    assert recorded[0] == {
        "profile_name": "my_profile",
        "region_name": "ap-southeast-1",
    }


def test_default_chain_when_nothing_is_set(monkeypatch):
    recorded = []

    class FakeSession:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

        def client(self, service_name, **kwargs):
            return f"fake_client_{service_name}"

    monkeypatch.setattr(boto3, "Session", FakeSession)

    client = make_bedrock_client(region="ap-southeast-1")
    assert client == "fake_client_bedrock-runtime"
    assert len(recorded) == 1
    assert recorded[0] == {
        "region_name": "ap-southeast-1",
    }


def test_build_llm_passes_settings_through(monkeypatch):
    recorded = []

    class FakeSession:
        def __init__(self, **kwargs):
            recorded.append(kwargs)

        def client(self, service_name, **kwargs):
            return f"fake_client_{service_name}"

    monkeypatch.setattr(boto3, "Session", FakeSession)

    settings = Settings(
        mode="dry_run",
        aws_region="ap-southeast-1",
        bedrock_model_id="test-model-id",
        aws_access_key_id="test_key_id",
        aws_secret_access_key="test_secret_key",
        aws_session_token="test_session_token",
        aws_profile="test_profile",
    )
    llm = build_llm(settings)
    assert isinstance(llm, BedrockLLM)
    assert llm.model_id == "test-model-id"
    assert llm.region == "ap-southeast-1"
    assert llm.client == "fake_client_bedrock-runtime"
    assert len(recorded) == 1
    assert recorded[0] == {
        "aws_access_key_id": "test_key_id",
        "aws_secret_access_key": "test_secret_key",
        "aws_session_token": "test_session_token",
        "region_name": "ap-southeast-1",
    }


def test_bedrock_client_retries_throttling_with_adaptive_backoff():
    client = make_bedrock_client(
        region="ap-southeast-1", access_key_id="AKIA_TEST", secret_access_key="secret_test"
    )
    retries = client.meta.config.retries
    assert retries["mode"] == "adaptive"
    assert retries.get("max_attempts", retries.get("total_max_attempts", 0) - 1) == 8
