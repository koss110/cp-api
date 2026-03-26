"""
API Service - DevOps Exam Costa
Receives requests from ELB, validates token, validates payload, publishes to SQS.
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator


# ==========================================
# Logging — JSON format
# ==========================================
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logging.root.handlers = [_handler]

# Keep AWS/HTTP internals quiet regardless of LOG_LEVEL
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ==========================================
# Configuration
# ==========================================
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
APP_VERSION = os.getenv("APP_VERSION", "unknown")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
SSM_PARAMETER_NAME = os.getenv("SSM_PARAMETER_NAME", "/devops-exam-costa/api/token")

# LocalStack support
LOCALSTACK_ENDPOINT = os.getenv("LOCALSTACK_ENDPOINT", "")
AWS_KWARGS: Dict[str, Any] = {"region_name": AWS_REGION}
if LOCALSTACK_ENDPOINT:
    AWS_KWARGS["endpoint_url"] = LOCALSTACK_ENDPOINT

# ==========================================
# AWS Clients
# ==========================================
ssm_client = boto3.client("ssm", **AWS_KWARGS)
sqs_client = boto3.client("sqs", **AWS_KWARGS)

# ==========================================
# Token Cache
# ==========================================
_cached_token: str | None = None


def get_api_token() -> str:
    """Fetch API token from SSM Parameter Store (cached in memory)."""
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    try:
        response = ssm_client.get_parameter(
            Name=SSM_PARAMETER_NAME,
            WithDecryption=True,
        )
        _cached_token = response["Parameter"]["Value"]
        logger.info("API token loaded from SSM: %s", SSM_PARAMETER_NAME)
        return _cached_token
    except (ClientError, BotoCoreError) as e:
        logger.error("Failed to load API token from SSM: %s", e)
        raise RuntimeError(f"Cannot load API token from SSM: {e}") from e


def invalidate_token_cache() -> None:
    """Clear cached token (used in tests and token rotation)."""
    global _cached_token
    _cached_token = None


# ==========================================
# Request / Response Models
# ==========================================
class EmailData(BaseModel):
    """The 4 required email fields."""

    email_subject: str = Field(..., min_length=1, description="Email subject")
    email_sender: str = Field(..., min_length=1, description="Sender name or address")
    email_timestream: str = Field(
        ..., min_length=1, description="Unix timestamp of the email"
    )
    email_content: str = Field(..., min_length=1, description="Email body content")

    @field_validator("email_subject", "email_sender", "email_content")
    @classmethod
    def no_blank_strings(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be blank")
        return v.strip()

    @field_validator("email_timestream")
    @classmethod
    def valid_timestamp(cls, v: str) -> str:
        """email_timestream must be a numeric unix timestamp string."""
        if not v.strip():
            raise ValueError("email_timestream cannot be blank")
        try:
            int(v.strip())
        except ValueError:
            raise ValueError(
                "email_timestream must be a valid unix timestamp (numeric string)"
            )
        return v.strip()


class MessageRequest(BaseModel):
    """
    Exam payload format:
    {
        "data": {
            "email_subject": "...",
            "email_sender": "...",
            "email_timestream": "1693561101",
            "email_content": "..."
        },
        "token": "<secret>"
    }
    """

    data: EmailData
    token: str = Field(
        ..., min_length=1, description="Auth token — validated against SSM"
    )


class MessageResponse(BaseModel):
    message_id: str
    status: str = "published"
    queue_url: str
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    service: str = "api"
    version: str = APP_VERSION


# ==========================================
# FastAPI Application
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load token on startup to fail fast if SSM is unavailable."""
    try:
        get_api_token()
        logger.info("Startup: API token loaded successfully")
    except RuntimeError as e:
        logger.warning(
            "Startup: Could not pre-load token: %s (will retry on first request)", e
        )
    yield


app = FastAPI(
    title="DevOps Exam API",
    description="Accepts email messages, validates token, publishes data to SQS.",
    version=APP_VERSION,
    lifespan=lifespan,
)


# ==========================================
# Endpoints
# ==========================================
@app.get("/healthz", response_model=HealthResponse, tags=["Health"])
def health_check() -> HealthResponse:
    """Health check — used by ALB target group."""
    return HealthResponse(status="healthy")


@app.post("/message", response_model=MessageResponse, tags=["Messages"])
def publish_message(request: MessageRequest) -> MessageResponse:
    """
    Validate token and publish email data to SQS.

    Request body:
        {
            "data": {
                "email_subject": "...",
                "email_sender": "...",
                "email_timestream": "1693561101",
                "email_content": "..."
            },
            "token": "<secret>"
        }
    """
    # 1. Validate token against SSM
    try:
        expected_token = get_api_token()
    except RuntimeError as e:
        logger.error("Cannot validate token — SSM unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Service unavailable") from e

    if request.token != expected_token:
        logger.warning("Invalid token attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 2. Publish data (not the token) to SQS
    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL not configured")
        raise HTTPException(status_code=503, detail="Queue not configured")

    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    message_body = {
        "message_id": message_id,
        "timestamp": timestamp,
        "email_subject": request.data.email_subject,
        "email_sender": request.data.email_sender,
        "email_timestream": request.data.email_timestream,
        "email_content": request.data.email_content,
    }

    try:
        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body),
            MessageAttributes={
                "source": {"StringValue": "api-service", "DataType": "String"},
            },
        )
        logger.info(
            "Message published: id=%s subject=%s",
            message_id,
            request.data.email_subject,
        )
    except (ClientError, BotoCoreError) as e:
        logger.error("Failed to publish to SQS: %s", e)
        raise HTTPException(status_code=503, detail="Failed to publish message") from e

    return MessageResponse(
        message_id=message_id,
        status="published",
        queue_url=SQS_QUEUE_URL,
        timestamp=timestamp,
    )
