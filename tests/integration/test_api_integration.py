"""
Integration tests for the API service using LocalStack.

These tests require a running LocalStack instance and are skipped automatically
when the LOCALSTACK_ENDPOINT environment variable is not set.

Run locally:
    docker-compose -f ../../cp-infra/iac/docker-compose.local.yml up -d
    LOCALSTACK_ENDPOINT=http://localhost:4566 pytest tests/integration/ -v
"""

import json
import os

import boto3
import pytest
import requests
from fastapi.testclient import TestClient

LOCALSTACK_ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "")

pytestmark = pytest.mark.skipif(
    not LOCALSTACK_ENDPOINT,
    reason="LOCALSTACK_ENDPOINT not set — skipping integration tests",
)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
QUEUE_NAME = "exam-costa-test-messages"
SSM_PARAM = "/exam-costa/test/api/token"
TEST_TOKEN = "integration-test-token"


# ==========================================
# LocalStack fixtures
# ==========================================


@pytest.fixture(scope="module")
def aws_clients():
    """Create real boto3 clients pointing at LocalStack."""
    kwargs = {
        "endpoint_url": LOCALSTACK_ENDPOINT,
        "region_name": AWS_REGION,
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }
    return {
        "sqs": boto3.client("sqs", **kwargs),
        "ssm": boto3.client("ssm", **kwargs),
    }


@pytest.fixture(scope="module")
def localstack_resources(aws_clients):
    """Create SQS queue and SSM parameter in LocalStack."""
    sqs = aws_clients["sqs"]
    ssm = aws_clients["ssm"]

    # Create SQS queue (or get URL if it already exists)
    try:
        queue_resp = sqs.create_queue(QueueName=QUEUE_NAME)
        queue_url = queue_resp["QueueUrl"]
    except sqs.exceptions.QueueNameExists:
        queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]

    # Create SSM parameter
    ssm.put_parameter(
        Name=SSM_PARAM,
        Value=TEST_TOKEN,
        Type="SecureString",
        Overwrite=True,
    )

    yield {"queue_url": queue_url, "ssm_param": SSM_PARAM}

    # Cleanup
    try:
        sqs.delete_queue(QueueUrl=queue_url)
    except Exception:
        pass
    try:
        ssm.delete_parameter(Name=SSM_PARAM)
    except Exception:
        pass


@pytest.fixture(scope="module")
def api_client(localstack_resources):
    """Create a FastAPI TestClient with LocalStack env vars."""
    os.environ["LOCALSTACK_ENDPOINT"] = LOCALSTACK_ENDPOINT
    os.environ["SQS_QUEUE_URL"] = localstack_resources["queue_url"]
    os.environ["SSM_PARAMETER_NAME"] = localstack_resources["ssm_param"]
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_REGION"] = AWS_REGION

    # Import app after env vars are set
    import importlib

    import app.main as main_module

    importlib.reload(main_module)
    main_module.invalidate_token_cache()

    from app.main import app

    return TestClient(app)


# ==========================================
# Tests
# ==========================================


def test_health_endpoint(api_client):
    """GET /health should return 200 with status=healthy."""
    response = api_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "api"


def test_message_requires_auth(api_client):
    """POST /message without token should return 403."""
    response = api_client.post(
        "/message",
        json={
            "name": "test",
            "category": "test",
            "value": 1.0,
            "description": "test",
        },
    )
    assert response.status_code in (401, 403)


def test_message_rejects_invalid_token(api_client):
    """POST /message with wrong token should return 401."""
    response = api_client.post(
        "/message",
        json={
            "name": "test",
            "category": "test",
            "value": 1.0,
            "description": "test",
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_message_published_to_sqs(api_client, aws_clients, localstack_resources):
    """POST /message with valid token should publish to SQS."""
    response = api_client.post(
        "/message",
        json={
            "name": "Integration Test",
            "category": "testing",
            "value": 99.5,
            "description": "Published via integration test",
        },
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert "message_id" in body

    # Verify message landed in SQS
    sqs = aws_clients["sqs"]
    msgs = sqs.receive_message(
        QueueUrl=localstack_resources["queue_url"],
        MaxNumberOfMessages=1,
        WaitTimeSeconds=2,
    ).get("Messages", [])
    assert len(msgs) == 1
    payload = json.loads(msgs[0]["Body"])
    assert payload["name"] == "Integration Test"
    assert payload["category"] == "testing"


def test_message_validates_payload(api_client):
    """POST /message with missing fields should return 422."""
    response = api_client.post(
        "/message",
        json={"name": "only name"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 422
