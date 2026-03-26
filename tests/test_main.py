"""
Unit tests for API service.
Uses mocked AWS clients — no real AWS calls.
"""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

VALID_TOKEN = "test-secret-token"

VALID_DATA = {
    "email_subject": "Happy new year!",
    "email_sender": "John Doe",
    "email_timestream": "1693561101",
    "email_content": "Just want to say... Happy new year!!!",
}

VALID_PAYLOAD = {
    "data": VALID_DATA,
    "token": VALID_TOKEN,
}


# ==========================================
# Fixtures
# ==========================================


@pytest.fixture(autouse=True)
def reset_token_cache():
    from app.main import invalidate_token_cache

    invalidate_token_cache()
    yield
    invalidate_token_cache()


@pytest.fixture
def mock_ssm():
    with patch("app.main.ssm_client") as mock:
        mock.get_parameter.return_value = {"Parameter": {"Value": VALID_TOKEN}}
        yield mock


@pytest.fixture
def mock_sqs():
    with patch("app.main.sqs_client") as mock:
        mock.send_message.return_value = {"MessageId": "test-msg-123"}
        yield mock


@pytest.fixture
def client(mock_ssm, mock_sqs):
    from app.main import app

    return TestClient(app)


# ==========================================
# Health Check
# ==========================================


class TestHealthCheck:
    def test_health_returns_200(self, client):
        assert client.get("/healthz").status_code == 200

    def test_health_returns_healthy(self, client):
        assert client.get("/healthz").json()["status"] == "healthy"

    def test_health_includes_service_name(self, client):
        assert client.get("/healthz").json()["service"] == "api"


# ==========================================
# Token Validation
# ==========================================


class TestTokenValidation:
    def test_valid_token_accepted(self, client):
        assert client.post("/message", json=VALID_PAYLOAD).status_code == 200

    def test_invalid_token_returns_401(self, client):
        payload = {**VALID_PAYLOAD, "token": "wrong-token"}
        assert client.post("/message", json=payload).status_code == 401

    def test_missing_token_returns_422(self, client):
        payload = {"data": VALID_DATA}
        assert client.post("/message", json=payload).status_code == 422

    def test_empty_token_returns_422(self, client):
        payload = {**VALID_PAYLOAD, "token": ""}
        assert client.post("/message", json=payload).status_code == 422


# ==========================================
# Payload Validation
# ==========================================


class TestPayloadValidation:
    def _post(self, client, data):
        return client.post("/message", json={"data": data, "token": VALID_TOKEN})

    def test_valid_payload_accepted(self, client):
        assert self._post(client, VALID_DATA).status_code == 200

    def test_missing_email_subject_returns_422(self, client):
        data = {k: v for k, v in VALID_DATA.items() if k != "email_subject"}
        assert self._post(client, data).status_code == 422

    def test_missing_email_sender_returns_422(self, client):
        data = {k: v for k, v in VALID_DATA.items() if k != "email_sender"}
        assert self._post(client, data).status_code == 422

    def test_missing_email_timestream_returns_422(self, client):
        data = {k: v for k, v in VALID_DATA.items() if k != "email_timestream"}
        assert self._post(client, data).status_code == 422

    def test_missing_email_content_returns_422(self, client):
        data = {k: v for k, v in VALID_DATA.items() if k != "email_content"}
        assert self._post(client, data).status_code == 422

    def test_blank_email_subject_returns_422(self, client):
        assert (
            self._post(client, {**VALID_DATA, "email_subject": "   "}).status_code
            == 422
        )

    def test_blank_email_sender_returns_422(self, client):
        assert self._post(client, {**VALID_DATA, "email_sender": ""}).status_code == 422

    def test_invalid_timestream_returns_422(self, client):
        assert (
            self._post(
                client, {**VALID_DATA, "email_timestream": "not-a-number"}
            ).status_code
            == 422
        )

    def test_blank_timestream_returns_422(self, client):
        assert (
            self._post(client, {**VALID_DATA, "email_timestream": "  "}).status_code
            == 422
        )

    def test_empty_body_returns_422(self, client):
        assert client.post("/message", json={}).status_code == 422


# ==========================================
# SQS Publishing
# ==========================================


class TestSQSPublishing:
    def test_message_published_to_sqs(self, client, mock_sqs):
        client.post("/message", json=VALID_PAYLOAD)
        mock_sqs.send_message.assert_called_once()

    def test_response_includes_message_id(self, client):
        data = client.post("/message", json=VALID_PAYLOAD).json()
        assert "message_id" in data
        assert len(data["message_id"]) == 36  # UUID

    def test_response_status_published(self, client):
        assert (
            client.post("/message", json=VALID_PAYLOAD).json()["status"] == "published"
        )

    def test_sqs_payload_contains_email_fields(self, client, mock_sqs):
        client.post("/message", json=VALID_PAYLOAD)
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["email_subject"] == VALID_DATA["email_subject"]
        assert body["email_sender"] == VALID_DATA["email_sender"]
        assert body["email_timestream"] == VALID_DATA["email_timestream"]
        assert body["email_content"] == VALID_DATA["email_content"]
        assert "message_id" in body
        assert "timestamp" in body

    def test_token_not_published_to_sqs(self, client, mock_sqs):
        """Token must never appear in the SQS message body."""
        client.post("/message", json=VALID_PAYLOAD)
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert "token" not in body

    def test_sqs_failure_returns_503(self, client, mock_sqs):
        from botocore.exceptions import ClientError

        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "SendMessage",
        )
        assert client.post("/message", json=VALID_PAYLOAD).status_code == 503

    def test_missing_queue_url_returns_503(self, mock_ssm, mock_sqs):
        with patch("app.main.SQS_QUEUE_URL", ""):
            from app.main import app

            tc = TestClient(app)
            assert tc.post("/message", json=VALID_PAYLOAD).status_code == 503


# ==========================================
# SSM Failure
# ==========================================


class TestSSMFailure:
    def test_ssm_failure_returns_503(self, mock_sqs):
        from botocore.exceptions import ClientError
        from app.main import app, invalidate_token_cache

        with patch("app.main.ssm_client") as mock_ssm:
            mock_ssm.get_parameter.side_effect = ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
                "GetParameter",
            )
            invalidate_token_cache()
            tc = TestClient(app)
            assert tc.post("/message", json=VALID_PAYLOAD).status_code == 503
