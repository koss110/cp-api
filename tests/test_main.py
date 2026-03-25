"""
Unit tests for API service.
Uses mocked AWS clients - no real AWS calls.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ==========================================
# Fixtures
# ==========================================
VALID_TOKEN = "test-secret-token"
VALID_PAYLOAD = {
    "name": "Test Item",
    "category": "test",
    "value": 42.0,
    "description": "Test description",
}


@pytest.fixture(autouse=True)
def reset_token_cache():
    """Reset token cache before each test."""
    from app.main import invalidate_token_cache
    invalidate_token_cache()
    yield
    invalidate_token_cache()


@pytest.fixture
def mock_ssm():
    """Mock SSM to return a known token."""
    with patch("app.main.ssm_client") as mock:
        mock.get_parameter.return_value = {
            "Parameter": {"Value": VALID_TOKEN}
        }
        yield mock


@pytest.fixture
def mock_sqs():
    """Mock SQS to accept messages."""
    with patch("app.main.sqs_client") as mock:
        mock.send_message.return_value = {"MessageId": "test-msg-123"}
        yield mock


@pytest.fixture
def client(mock_ssm, mock_sqs):
    """TestClient with mocked AWS."""
    from app.main import app
    return TestClient(app)


# ==========================================
# Health Check Tests
# ==========================================
class TestHealthCheck:
    def test_health_returns_200(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_health_returns_healthy(self, client):
        response = client.get("/healthz")
        assert response.json()["status"] == "healthy"

    def test_health_includes_service_name(self, client):
        response = client.get("/healthz")
        assert response.json()["service"] == "api"


# ==========================================
# Authentication Tests
# ==========================================
class TestAuthentication:
    def test_valid_token_accepted(self, client):
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.status_code == 200

    def test_invalid_token_returns_401(self, client):
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_missing_token_returns_403(self, client):
        response = client.post("/message", json=VALID_PAYLOAD)
        assert response.status_code == 403

    def test_malformed_auth_header_returns_403(self, client):
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": "NotBearer token"},
        )
        assert response.status_code == 403


# ==========================================
# Payload Validation Tests
# ==========================================
class TestPayloadValidation:
    def _post(self, client, payload):
        return client.post(
            "/message",
            json=payload,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )

    def test_valid_payload_accepted(self, client):
        response = self._post(client, VALID_PAYLOAD)
        assert response.status_code == 200

    def test_missing_name_returns_422(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "name"}
        response = self._post(client, payload)
        assert response.status_code == 422

    def test_missing_category_returns_422(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "category"}
        response = self._post(client, payload)
        assert response.status_code == 422

    def test_missing_value_returns_422(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "value"}
        response = self._post(client, payload)
        assert response.status_code == 422

    def test_missing_description_returns_422(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "description"}
        response = self._post(client, payload)
        assert response.status_code == 422

    def test_empty_name_returns_422(self, client):
        response = self._post(client, {**VALID_PAYLOAD, "name": "   "})
        assert response.status_code == 422

    def test_empty_category_returns_422(self, client):
        response = self._post(client, {**VALID_PAYLOAD, "category": ""})
        assert response.status_code == 422

    def test_empty_body_returns_422(self, client):
        response = self._post(client, {})
        assert response.status_code == 422

    def test_value_as_string_accepted(self, client):
        """Pydantic coerces numeric strings."""
        response = self._post(client, {**VALID_PAYLOAD, "value": "99.5"})
        assert response.status_code == 200

    def test_negative_value_accepted(self, client):
        response = self._post(client, {**VALID_PAYLOAD, "value": -10.0})
        assert response.status_code == 200


# ==========================================
# SQS Publishing Tests
# ==========================================
class TestSQSPublishing:
    def test_message_published_to_sqs(self, client, mock_sqs):
        client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        mock_sqs.send_message.assert_called_once()

    def test_response_includes_message_id(self, client):
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        data = response.json()
        assert "message_id" in data
        assert len(data["message_id"]) == 36  # UUID format

    def test_response_status_published(self, client):
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.json()["status"] == "published"

    def test_sqs_payload_contains_all_fields(self, client, mock_sqs):
        client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        call_kwargs = mock_sqs.send_message.call_args[1]
        body = json.loads(call_kwargs["MessageBody"])
        assert body["name"] == VALID_PAYLOAD["name"]
        assert body["category"] == "test"  # lowercased
        assert body["value"] == VALID_PAYLOAD["value"]
        assert body["description"] == VALID_PAYLOAD["description"]
        assert "message_id" in body
        assert "timestamp" in body

    def test_sqs_failure_returns_503(self, client, mock_sqs):
        from botocore.exceptions import ClientError
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "SendMessage",
        )
        response = client.post(
            "/message",
            json=VALID_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.status_code == 503

    def test_missing_queue_url_returns_503(self, mock_ssm, mock_sqs):
        with patch("app.main.SQS_QUEUE_URL", ""):
            from app.main import app
            tc = TestClient(app)
            response = tc.post(
                "/message",
                json=VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert response.status_code == 503


# ==========================================
# SSM Failure Tests
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
            response = tc.post(
                "/message",
                json=VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert response.status_code == 503
