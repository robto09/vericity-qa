# Vericity QA Framework — Journey & Architecture

> A running log of **how we built the QA framework, what we validate for each service,
> and WHY** — so that, in an interview, you can explain not just "I wrote tests" but
> "here's the strategy, here's what can break in each AWS service, and here's how I prove
> it works."

This repo (`vericity-qa`) tests the platform in the parent directory (`vericity-platform`).
The platform is the *system under test*; this is the *test framework*.

---

## 1. Philosophy: test the REAL system

These tests run against the **live, deployed AWS stack** — real API Gateway, real
DynamoDB, real EventBridge, real Step Functions, real SQS/DLQ, real RDS — not mocks.

**Why live, not mocked?** The hardest, most valuable things to validate in a distributed
serverless system are the *seams between services*: does the event actually reach the
consumer? does a failure actually land in the DLQ? does data stay consistent as it flows
API → DynamoDB → workflow → S3 → SQS → RDS? Mocks can't prove any of that — they only
prove your code does what you told it to in isolation. Live tests prove the *system* works.

(We still unit-test pure logic with no AWS — that lives in the platform repo's `tests/`.
This repo is the integration/E2E layer that mocks can't replace.)

**How tests find the system:** `framework/config.py` reads the deployed CloudFormation
stack's **Outputs** (API URL, table names, queue URLs, ARNs) at runtime. Nothing is
hardcoded, so the same suite runs against any environment by changing one env var
(`VERICITY_STACK`).

---

## 2. The core challenge: asynchronous, eventually-consistent

The system is event-driven. When you submit an application:

```
POST /applications → DynamoDB write → EventBridge(ApplicationSubmitted)
   → Step Functions (parallel checks → score → decision) → EventBridge(PolicyApproved)
   → Issuance Lambda → S3 + EventBridge(PolicyIssued) → SQS → Billing Lambda → RDS
```

The final state (policy ACTIVE, billing row in RDS) appears **seconds later**, after all
of that runs. So tests can't check the result immediately — they must **poll until a
condition is met, with a timeout**. That's `framework/wait.py`:

- `wait_until(condition, timeout, ...)` — poll until truthy or fail. The backbone of every
  async assertion. (Returns as soon as the condition is met → fast on success, only burns
  the timeout on real failure. Far better than `sleep(30)` everywhere.)
- `stays_true(condition, duration, ...)` — assert something *stays* true for a window.
  Used for negative async claims you can't prove instantly (e.g. "no duplicate row ever
  appears", "message does NOT reach the DLQ").

**Interview soundbite:** "Testing an async process means asserting under eventual
consistency — poll for the expected outcome with a timeout, and for negative cases, verify
the bad outcome doesn't occur over a window. Never a fixed sleep; that's slow and flaky."

---

## 3. The EventBridge problem & the probe pattern

EventBridge events are fire-and-forget — there's no "read the last events" API. To
validate "was the event published with the right shape?", you must **subscribe a test
consumer before triggering the producer**.

`framework/event_probe.py` implements the standard technique:
1. Create a temporary SQS queue (the probe).
2. Add a temporary EventBridge rule routing the target detail-types to that queue.
3. Trigger the producer.
4. Read the queue → assert on the captured events.
5. Tear down the rule + queue.

This one tool answers six of the validation requirements: *event published correctly,
event schema correct, received by consumers, duplicate events, missing events, out-of-order
events.*

---

## 4. Testing types (and where each lives)

| Type | What it means here | Folder |
|------|--------------------|--------|
| **Functional** | One service behaves per spec (e.g. quote pricing, validation rejects bad input) | `tests/functional/` |
| **Integration** | Two+ services work together (API write → DynamoDB read; event → consumer) | `tests/integration/` |
| **Message validation** | Events/messages have the correct schema, source, detail-type, payload | `tests/events/` |
| **Retry & error-handling** | Transient failures recover; terminal failures route to DLQ / Catch fallback | `tests/async/`, `tests/workflow/` |
| **Monitoring / log verification** | The system logged the expected events to CloudWatch (observability is testable) | `tests/monitoring/` |
| **Workflow** | Step Functions takes the right path; each state behaves; retries/catch work | `tests/workflow/` |
| **Data integrity** | The same facts are consistent across DynamoDB, S3, and RDS; no loss | `tests/data/` |
| **Pipeline** | Bulk data migration reconciles (no silent drops), flags dirty rows, fails on drift | `tests/pipeline/` |

---

## 5. What we validate per service — and WHY

### API Gateway + Lambda + DynamoDB (`tests/functional`, `tests/integration`)
**What:** happy-path responses (201 + correct body), validation errors (400 with every
field error), boundary values (age 18/75, coverage limits), and that a created
quote/application is actually persisted in DynamoDB with the right fields.
**Why:** this is the front door — if input validation or persistence is wrong, everything
downstream is poisoned. Boundaries are where bugs hide (off-by-one on age limits, etc.).
**Answers:** *"How would you automate API and backend testing using PyTest/Postman?"*

### EventBridge (`tests/events`)
**What:** submitting an application publishes exactly one `ApplicationSubmitted` event with
the correct `source`, `detail-type`, and a `detail` matching the documented schema; the
approve/decline path publishes the right event; no duplicate events; ordering across the
lifecycle (`ApplicationSubmitted` before `PolicyApproved` before `PolicyIssued`).
**Why:** events are the glue between every stage. A malformed, missing, duplicated, or
mis-ordered event silently breaks a downstream stage with no error at the source.
**Answers:** *"How would you validate events published to EventBridge?"*

### Step Functions underwriting workflow (`tests/workflow`)
**What:** start an execution with a known input, poll to completion, assert (a) final
status, (b) the *execution history* took the expected path (approve vs decline branch), and
(c) the error path — inject a failure and confirm it lands in the `ManualReview` Catch
state, NOT a failed execution. Also test each state's Lambda in isolation via direct invoke.
**Why:** a workflow's value is its branching and error handling; those are exactly what
breaks. The execution history makes every branch/retry/catch an assertable fact.
**Answers:** *"How would you test an AWS Lambda workflow?"*

### SQS + DLQ + Billing (`tests/async`)
**What:** a `PolicyIssued` event results in a billing message processed into RDS (async,
verified by polling); a deliberately-failing message is retried and lands in the **DLQ**
after maxReceiveCount, while good messages are unaffected; the same message sent twice
produces exactly one ledger row (idempotency); a bad message in a batch doesn't fail the
good ones (partial-batch).
**Why:** async + retries + DLQ is where "it works in the demo but loses data in prod"
happens. At-least-once delivery means duplicates are real; poison messages must not be
lost OR retried forever.
**Answers:** *"How would you test an SQS-based asynchronous process?"* and *"How would you
test retries and dead-letter queues (DLQs)?"*

### Cross-service data integrity (`tests/data`)
**What:** run one application end-to-end, then assert the SAME identity and figures are
consistent everywhere — the DynamoDB policy row, the S3 policy document contents, and the
RDS ledger amount all agree; counts reconcile (one application → one document → one first
premium).
**Why:** each stage re-reads/re-writes data; a premium recomputed differently at one stage,
or an orphaned record from a lost event, corrupts the customer's actual policy.
**Answers:** *"How would you verify data integrity across services?"*

### Migration pipeline (`tests/pipeline`)
**What:** upload the sample legacy CSV; assert the migration report reconciles
(`rows_read == rows_migrated + rows_flagged`, no silent drops), that exactly the dirty rows
are flagged with correct reasons, and that migrated rows appear in DynamoDB. Negative:
a CSV with a renamed column fails fast (schema drift).
**Why:** "data migration integrity" is a core job requirement; the #1 migration bug is
silently dropping records, so the reconciliation invariant is the headline assertion.
**Answers:** *"How would you validate data moving through a pipeline?"*

### CloudWatch monitoring (`tests/monitoring`)
**What:** after triggering a flow, assert the expected structured log lines exist in the
relevant Lambda's CloudWatch log group (e.g. billing logged "First premium recorded" with
the right policy_id; underwriting logged a risk score).
**Why:** in a distributed system you often can't observe internal steps directly — logs
ARE the observability surface. If you can't assert on logs, you can't prove what happened
inside an async Lambda. Also validates the system is *operationally* observable.
**Answers:** the "Monitoring and log verification (CloudWatch)" testing type.

---

## 6. Service-to-service & failure-mode coverage map

The specific concerns you wanted covered, mapped to where they're tested:

| Concern | Where | How |
|---------|-------|-----|
| Service-to-service communication | `tests/integration`, `tests/events` | trigger producer, assert consumer received/acted |
| Network failures | `tests/workflow` (retries) | Step Functions Retry recovers transient Task failures |
| Retry logic | `tests/async`, `tests/workflow` | force failure, assert eventual success or DLQ |
| Partial failures | `tests/async` | bad message in a batch doesn't fail the good ones |
| Data consistency | `tests/data` | cross-store reconciliation after one E2E run |
| Message delivery | `tests/async`, `tests/events` | probe/poll the consumer side |
| Event published correctly | `tests/events` | probe captures it; assert presence |
| Event schema correct | `tests/events` | jsonschema-validate the `detail` |
| Event received by consumers | `tests/integration` | assert the consumer's side effect happened |
| Duplicate events | `tests/events`, `tests/async` | collect over a window; assert no dup; idempotency on dup |
| Missing events | `tests/events` | probe times out → explicit "missing event" failure |
| Out-of-order events | `tests/events` | capture lifecycle events; assert causal ordering |

---

## 7. Build log

### Phase Q0/Q1 — foundation
- Scaffolded the repo (`framework/` utilities, `tests/` by concern, `conftest.py`, pytest config).
- `framework/config.py` — resolves the live stack from CloudFormation Outputs (portable).
- `framework/wait.py` — `wait_until` / `stays_true` for eventual-consistency assertions.
- `framework/api.py` — HTTP client for the REST API.
- `framework/data.py` — DynamoDB + RDS accessors for data validation.
- `framework/event_probe.py` — the EventBridge capture pattern.
- This journey/architecture doc.

### Phase Q2 — API + DynamoDB (functional + integration)
- `tests/functional/test_quote_api.py` — happy path, contract (jsonschema), known
  premiums (BR-2.5), tobacco doubling, and **boundary testing** (just-inside vs
  just-outside age/coverage/term limits, both accepted AND rejected sides).
- `tests/integration/test_api_dynamodb.py` — quote/application actually persisted with
  matching fields; unknown policy → 404.
- **Result: 20 passed against the live stack in ~9s.**

### Phase Q3 — EventBridge (the probe pattern)
- `tests/events/test_eventbridge.py` — event published, envelope (source/detail-type),
  detail schema, no duplicates, causal ordering across the lifecycle.
- **Bug found & fixed in the framework:** the probe used a fixed `sleep(3)` after creating
  the temporary rule, which was unreliable — EventBridge rule propagation is variable, so
  some runs captured 0 events (flaky). Fixed `event_probe.py` to **actively verify the
  rule is live** before returning: it publishes a marker event and polls until the marker
  arrives, proving the rule→queue path works. This is itself the lesson: *don't sleep-and-
  hope for async infra to be ready — verify it.*
- **Result: 6 passed.**

### Phase Q4 — Step Functions workflow
- `framework/stepfunctions.py` — start execution, wait for completion, read the visited-
  states list from execution history.
- `tests/workflow/test_underwriting_workflow.py` — approve path & decline path (asserted
  via execution history: the right branch was taken), parallel-checks + merge visited, and
  the **error fallback** (malformed input → ManualReview via Catch, execution SUCCEEDS not
  FAILS). Added a `seeded_policy` conftest fixture that creates+cleans real DynamoDB rows.
- **Result: 5 passed**, including the resilience/Catch test.

### Phase Q5 — SQS + DLQ
- `framework/sqs.py` — send/inspect/drain queues.
- `tests/async/test_sqs_billing.py` — async happy path (message → policy ACTIVE),
  idempotency (duplicate message stays correct), and **retry → DLQ** (poison message
  retried 3× then lands in the DLQ, ~3 min, verified by polling the DLQ).
- **Result: 3 passed.**

### Phase Q6 — cross-service data integrity
- `tests/data/test_data_integrity.py` — one E2E application; assert the SAME facts are
  consistent across DynamoDB ↔ the S3 document contents ↔ the key convention; declined
  policy gets NO document (decline branch leaks no artifacts).
- **Result: 2 passed.**

### Phase Q7 — migration pipeline
- `framework/logs.py` — CloudWatch log search + JSON-context extraction.
- `tests/pipeline/test_migration_pipeline.py` — upload a legacy CSV; read the migration
  report from CloudWatch; assert the **reconciliation invariant** (read == migrated +
  flagged), exact 7/3 split, every flagged row has a reason; schema-drift file rejected.
- **Result: 2 passed.**

### Phase Q8 — CloudWatch monitoring / observability
- `tests/monitoring/test_cloudwatch_logs.py` — underwriting logged the risk score;
  billing logged "First premium recorded" with the right context.
- **Bug found & fixed in a test:** logs are themselves eventually-consistent, so a
  read-once check raced the log write — fixed to **poll until OUR policy's log line
  appears** (same eventual-consistency principle the framework is built on).
- **Result: 2 passed.**

### Phase Q9 — Postman + CI
- `postman/vericity.postman_collection.json` + `run-newman.sh` — the API suite as a
  shareable, Newman-runnable collection (resolves the live API URL automatically).
- `.github/workflows/qa.yml` — CI quality gate: fast tests on every push/PR, full slow
  suite on schedule/dispatch; AWS creds from secrets.
- `README.md` — how to run everything.

### Summary
**40 tests, all passing against the live AWS stack.** Every interview question and every
validation concern (service-to-service, network failures/retries, partial failures, data
consistency, message delivery, event published/schema/received/duplicate/missing/order,
DLQ) has at least one concrete, passing test pointing at a real component.

Two real framework lessons emerged and are baked in: (1) for async infra, **verify it's
ready, don't sleep-and-hope**; (2) **logs are eventually-consistent too** — poll for the
specific line you expect.

---

## 8. Running the suite

```bash
cd vericity-qa
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Ensure the platform stack is deployed AND RDS is started (see platform repo).
# Then run everything, or a subset by marker:
.venv/bin/pytest                       # all tests
.venv/bin/pytest -m functional         # just functional
.venv/bin/pytest -m "events or async"  # event + async tests
.venv/bin/pytest -m "not slow"         # skip the slow async-propagation tests
```

> Prerequisite: the platform stack must be deployed and **RDS running**
> (`../scripts/db-power.sh start`) — these tests hit real resources.
