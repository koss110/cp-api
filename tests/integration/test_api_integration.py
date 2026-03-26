"""
Integration tests for the API service using LocalStack.

Skipped automatically when LOCALSTACK_ENDPOINT is not set.

Run locally:
    docker-compose -f ../../cp-infra/local/docker-compose.yml up -d localstack localstack-init
    LOCALSTACK_ENDPOINT=http://localhost:4566 make test-integration
"""

import json
import os

import boto3
import pytest
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

VALID_DATA = {
    "email_subject": "Happy new year!",
    "email_sender": "John Doe",
    "email_timestream": "1693561101",
    "email_content": "Just want to say... Happy new year!!!",
}


# ==========================================
# Fixtures
# ==========================================


@pytest.fixture(scope="module")
def aws_clients():
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
    sqs = aws_clients["sqs"]
    ssm = aws_clients["ssm"]

    # Create SQS queue (or get URL if it already exists)
    try:
        queue_resp = sqs.create_queue(QueueName=QUEUE_NAME)
        queue_url = queue_resp["QueueUrl"]
    except sqs.exceptions.QueueNameExists:
        queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]

    # Seed SSM token
    ssm.put_parameter(
        Name=SSM_PARAM, Value=TEST_TOKEN, Type="SecureString", Overwrite=True
    )

    yield {"queue_url": queue_url}

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
    os.environ["LOCALSTACK_ENDPOINT"] = LOCALSTACK_ENDPOINT
    os.environ["SQS_QUEUE_URL"] = localstack_resources["queue_url"]
    os.environ["SSM_PARAMETER_NAME"] = SSM_PARAM
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_REGION"] = AWS_REGION

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
    r = api_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_message_requires_token(api_client):
    r = api_client.post("/message", json={"data": VALID_DATA})
    assert r.status_code == 422


def test_message_rejects_invalid_token(api_client):
    r = api_client.post("/message", json={"data": VALID_DATA, "token": "wrong"})
    assert r.status_code == 401


def test_message_published_to_sqs(api_client, aws_clients, localstack_resources):
    r = api_client.post("/message", json={"data": VALID_DATA, "token": TEST_TOKEN})
    assert r.status_code == 200
    assert r.json()["status"] == "published"

    # Verify message landed in SQS
    msgs = (
        aws_clients["sqs"]
        .receive_message(
            QueueUrl=localstack_resources["queue_url"],
            MaxNumberOfMessages=1,
            WaitTimeSeconds=2,
        )
        .get("Messages", [])
    )
    assert len(msgs) == 1
    body = json.loads(msgs[0]["Body"])
    assert body["email_subject"] == VALID_DATA["email_subject"]
    assert body["email_sender"] == VALID_DATA["email_sender"]
    assert "token" not in body


def test_message_validates_payload(api_client):
    r = api_client.post(
        "/message",
        json={"data": {"email_subject": "only one field"}, "token": TEST_TOKEN},
    )
    assert r.status_code == 422
