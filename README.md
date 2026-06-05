# Vericity QA Framework

Automated QA for the **Vericity policy platform** (the system in the parent directory).
Tests run against the **live deployed AWS stack** — real API Gateway, DynamoDB,
EventBridge, Step Functions, SQS/DLQ, S3, and RDS — validating the things mocks can't:
service-to-service communication, async delivery, retries, DLQ behavior, and data
consistency across services.

See **[QA_FRAMEWORK_JOURNEY.md](QA_FRAMEWORK_JOURNEY.md)** for the architecture, the
testing strategy, and *why* we validate what we do for each service.

## Documentation

| Doc | What it covers |
|-----|----------------|
| [QA_FRAMEWORK_JOURNEY.md](QA_FRAMEWORK_JOURNEY.md) | Architecture, testing types, why each validation per service, build log |
| [LESSONS.md](LESSONS.md) | Every gotcha we hit (flaky async, EventBridge propagation, log lag, VPC reachability) with symptom/fix/interview takeaway |
| [docs/CI_SETUP.md](docs/CI_SETUP.md) | Exactly what's needed to run this in GitHub Actions (git, secrets, stack) |

## What it validates (and the interview questions it answers)

| Test area | Folder | Answers |
|-----------|--------|---------|
| API functional + validation + boundaries | `tests/functional/` | "Automate API/backend testing with PyTest/Postman?" |
| API → DynamoDB persistence | `tests/integration/` | service-to-service, data persistence |
| EventBridge publish / schema / delivery / duplicates / ordering | `tests/events/` | "Validate events published to EventBridge?" |
| Step Functions paths / retries / Catch fallback | `tests/workflow/` | "Test an AWS Lambda workflow?" |
| SQS async / retries / DLQ / idempotency | `tests/async/` | "Test an SQS async process?" + "Test retries and DLQs?" |
| Cross-service data integrity (DynamoDB ↔ S3 ↔ RDS) | `tests/data/` | "Verify data integrity across services?" |
| Migration pipeline reconciliation | `tests/pipeline/` | "Validate data moving through a pipeline?" |
| CloudWatch log / observability verification | `tests/monitoring/` | "Monitoring and log verification (CloudWatch)" |

## Testing types covered

Functional · Integration (service-to-service) · Message validation · Retry &
error-handling · Monitoring / log verification.

## Prerequisites

1. The platform stack must be **deployed** and **RDS running**:
   ```bash
   cd ..               # the platform repo
   ./scripts/deploy.sh          # if not already deployed
   ./scripts/db-power.sh start   # if RDS is stopped
   ```
2. AWS credentials configured (`aws configure`) — the same ones used to deploy.

## Setup

```bash
cd vericity-qa
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

The suite discovers the live stack automatically from CloudFormation outputs — no URLs
to hardcode. Override the target stack with `VERICITY_STACK` if needed.

```bash
# Fast tests only (functional + integration) — seconds, great for quick checks:
.venv/bin/pytest -m "not slow"

# Everything (includes the slow async/event/workflow tests — several minutes on real AWS):
.venv/bin/pytest

# By area:
.venv/bin/pytest -m functional
.venv/bin/pytest -m events
.venv/bin/pytest -m "async_ or workflow"
.venv/bin/pytest tests/data

# With an HTML report (handy for handoff):
.venv/bin/pytest -m "not slow" --html=report.html --self-contained-html
```

> **Heads up — the slow tests are genuinely slow** (2-4 min each) because they wait on
> real async propagation: EventBridge rules, Step Functions executions, SQS retries (the
> DLQ test waits ~3 min for a message to exhaust retries). That's the cost of testing the
> real system. Use `-m "not slow"` for fast iteration.

## Postman / Newman

A Postman collection mirrors the API tests for a shareable, CLI-runnable suite:

```bash
# Needs newman: npm install -g newman
./postman/run-newman.sh        # resolves the live API URL and runs the collection
```

## CI

`.github/workflows/qa.yml` runs the fast tests on every push/PR (a quality gate) and the
full suite on a daily schedule / manual dispatch. AWS creds come from repo secrets.

## Layout

```
framework/        reusable test utilities (NOT tests)
  config.py         resolve live stack from CloudFormation outputs
  aws.py            cached boto3 clients
  wait.py           wait_until / stays_true — eventual-consistency polling (the backbone)
  api.py            HTTP client for the REST API
  data.py           DynamoDB + RDS accessors
  event_probe.py    capture EventBridge events for assertion
  stepfunctions.py  drive + inspect Step Functions executions
  sqs.py            send / inspect SQS + DLQ
  logs.py           CloudWatch log assertions
  schemas.py        JSON schemas for contract testing
  models.py         test-data helpers
tests/            tests grouped by concern (see table above)
postman/          Postman collection + Newman runner
conftest.py       pytest fixtures (config, api, seeded_policy) + markers
```

## Key technique: testing asynchronous systems

The whole system is event-driven, so results appear *seconds later*. Tests never use
fixed sleeps; they **poll until a condition is met, with a timeout** (`wait_until`), and
for negative cases verify a bad outcome **doesn't** occur over a window (`stays_true`).
For EventBridge, a temporary SQS "probe" subscribes to the bus before the producer fires,
so published events can be captured and asserted. See the journey doc for the full
rationale — it's the core of how you'd answer "how do you test an async process?"
