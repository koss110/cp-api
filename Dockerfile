# Multi-stage build for API service
FROM python:3.12-slim AS base

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ==========================================
# Builder stage
# ==========================================
FROM base AS builder

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==========================================
# Production stage
# ==========================================
FROM base AS production

ARG BUILD_DATE
ARG VERSION
ARG VCS_REF

ENV APP_VERSION=${VERSION:-unknown}

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.title="devops-exam-api" \
      org.opencontainers.image.description="DevOps Exam API Service"

# Copy installed packages into standard system path
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/
COPY log_config.json ./

# Run as non-root user
RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENV AWS_SECRET_ACCESS_KEY=mysupersecretpassword
env REG_PASS=ghp_7OUWimLMrZt4Atk7tg9uihoiugyiuhuasd

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "log_config.json"]

# DUMMY COMMIT1
