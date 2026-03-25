"""
API Service - DevOps Exam Costa
Receives HTTP requests, validates token, validates payload, publishes to SQS
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, validator

# ==========================================
# Logging
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ==========================================
# Configuration
# ==========================================
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
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
    """Fetch API token from SSM Parameter Store (cached)."""
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
    """Clear token cache (useful for tests or token rotation)."""
    global _cached_token
    _cached_token = None


# ==========================================
# Request/Response Models
# ==========================================
class MessageRequest(BaseModel):
    """Incoming message payload - all 4 fields are required."""

    name: str = Field(..., min_length=1, max_length=255, description="Message name")
    category: str = Field(..., min_length=1, max_length=100, description="Message category")
    value: float = Field(..., description="Numeric value associated with the message")
    description: str = Field(..., min_length=1, max_length=1000, description="Message description")

    @validator("name", "category", "description")
    def no_empty_strings(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be blank")
        return v.strip()

    @validator("category")
    def valid_category(cls, v: str) -> str:
        return v.strip().lower()


class MessageResponse(BaseModel):
    """Response after successfully publishing a message."""

    message_id: str
    status: str = "published"
    queue_url: str
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    service: str = "api"
    version: str = "1.0.0"


# ==========================================
# FastAPI Application
# ==========================================
app = FastAPI(
    title="DevOps Exam API",
    description="Message API that publishes to SQS",
    version="1.0.0",
)

security = HTTPBearer()


@app.on_event("startup")
async def startup_event() -> None:
    """Pre-load API token on startup to fail fast if SSM is unavailable."""
    try:
        get_api_token()
        logger.info("Startup: API token loaded successfully")
    except RuntimeError as e:
        logger.warning("Startup: Could not load API token: %s (will retry on request)", e)


# ==========================================
# Endpoints
# ==========================================
@app.get("/healthz", response_model=HealthResponse, tags=["Health"])
def health_check() -> HealthResponse:
    """Health check endpoint - used by ALB target group."""
    return HealthResponse(status="healthy")


@app.post("/message", response_model=MessageResponse, tags=["Messages"])
def publish_message(
    request: MessageRequest,
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> MessageResponse:
    """
    Publish a validated message to SQS.

    Requires:
    - Authorization: Bearer <token>
    - JSON body with: name, category, value, description
    """
    # Validate token
    try:
        expected_token = get_api_token()
    except RuntimeError as e:
        logger.error("Token validation error: %s", e)
        raise HTTPException(status_code=503, detail="Service unavailable") from e

    if credentials.credentials != expected_token:
        logger.warning("Invalid token attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Publish to SQS
    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL not configured")
        raise HTTPException(status_code=503, detail="Queue not configured")

    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    message_body = {
        "message_id": message_id,
        "timestamp": timestamp,
        "name": request.name,
        "category": request.category,
        "value": request.value,
        "description": request.description,
    }

    try:
        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body),
            MessageAttributes={
                "source": {
                    "StringValue": "api-service",
                    "DataType": "String",
                },
                "category": {
                    "StringValue": request.category,
                    "DataType": "String",
                },
            },
        )
        logger.info("Message published: id=%s category=%s", message_id, request.category)
    except (ClientError, BotoCoreError) as e:
        logger.error("Failed to publish to SQS: %s", e)
        raise HTTPException(status_code=503, detail="Failed to publish message") from e

    return MessageResponse(
        message_id=message_id,
        status="published",
        queue_url=SQS_QUEUE_URL,
        timestamp=timestamp,
    )
