# cp-api

![CI](https://github.com/koss110/cp-api/actions/workflows/ci.yml/badge.svg)
![Release](https://github.com/koss110/cp-api/actions/workflows/release.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688)
![License](https://img.shields.io/badge/license-MIT-green)

REST API microservice — receives email messages over HTTP, validates a bearer token against AWS SSM Parameter Store, and publishes the payload to SQS.

---

## Architecture

```mermaid
flowchart LR
    client([Client]) -->|POST /message| alb[ALB]
    alb --> api[cp-api\nECS Fargate]
    api -->|GetParameter\nWithDecryption| ssm[(SSM\n/exam-costa/api/token)]
    api -->|SendMessage| sqs[(SQS Queue)]
    sqs --> worker[cp-worker\nECS Fargate]
```

---

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/healthz` | None | ALB health check |
| `POST` | `/message` | Token in body | Validate + publish to SQS |

### POST /message

**Request**
```json
{
  "data": {
    "email_subject": "Happy new year!",
    "email_sender": "John Doe",
    "email_timestream": "1693561101",
    "email_content": "Just want to say... Happy new year!!!"
  },
  "token": "<secret>"
}
```

**Responses**

| Status | Meaning |
|--------|---------|
| `200` | Message published to SQS |
| `401` | Invalid token |
| `422` | Missing or malformed fields |
| `503` | SSM or SQS unavailable |

---

## CI/CD

```mermaid
flowchart TD
    push[Push to branch] --> lint[Lint\nruff]
    lint --> unit[Unit Tests\npytest + coverage]
    unit --> integration[Integration Tests\nLocalStack]
    integration --> done[CI Pass]

    tag[Push tag vX.Y.Z] --> build[Build Docker image]
    build --> ecr[Push to ECR]
    ecr --> tfvars[Update image_tags\nstaging + production tfvars]
    tfvars --> staging[Staging deploy\nvia cp-infra main]
    tfvars --> pr[Open/update PR\nmain → production]
    pr --> prod[Production deploy\non PR merge]
```

- **CI** runs on every push — lint, unit tests, integration tests against LocalStack
- **Release** triggers on `v*.*.*` tag push — builds image, pushes to ECR, updates `cp-infra` tfvars, opens production PR

---

## Local development

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Run unit tests
make test-unit

# Run integration tests (requires LocalStack)
cd ../cp-infra && make local-up
LOCALSTACK_ENDPOINT=http://localhost:4566 make test-integration

# Run the API locally
uvicorn app.main:app --reload
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-east-2` | AWS region |
| `SQS_QUEUE_URL` | — | SQS queue URL |
| `SSM_PARAMETER_NAME` | `/devops-exam-costa/api/token` | SSM path for API token |
| `LOCALSTACK_ENDPOINT` | — | Set to use LocalStack instead of AWS |
| `LOG_LEVEL` | `INFO` | Log level |
| `APP_VERSION` | `unknown` | Injected at build time via `--build-arg VERSION` |

---

## Deploying a specific tag

```bash
# Trigger release workflow for an existing tag
gh workflow run release.yml \
  --repo koss110/cp-api \
  --field image_tag=v1.0.1 \
  --field open_pr=true
```
