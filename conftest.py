"""
Pytest configuration for API service tests.
Sets environment variables before any test module imports.
"""

import os

# Set test environment variables before importing app modules
os.environ.setdefault("SQS_QUEUE_URL", "https://sqs.us-east-2.amazonaws.com/123456789012/test-queue")
os.environ.setdefault("SSM_PARAMETER_NAME", "/devops-exam-costa/api/token")
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
