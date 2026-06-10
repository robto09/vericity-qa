# Lessons & Gotchas — Building QA for a Serverless System

Every real problem we hit building and running this QA framework, in skimmable form:
**symptom → cause → fix → the interview-ready takeaway.** These are exactly the
"tell me about a time you debugged a flaky/async test" stories that land in interviews —
and the reasoning behind how the framework is built.

> These come from testing the **live AWS stack**. You only learn most of them by testing
> the real thing — mocks hide every one of these.

---

## 1. Flaky async tests: don't `sleep()` — poll until true

**Symptom:** a test that submits an application and immediately checks the result fails
intermittently — the policy is still `APPLIED` because underwriting hasn't finished yet.

**Cause:** the system is **eventually consistent**. The result appears seconds later, after
EventBridge → Step Functions → more Lambdas run. Checking immediately is a race.

**Fix:** `wait_until(condition, timeout)` — poll until the expected outcome is true, with a
timeout. Returns as soon as it's satisfied (fast on success) and only burns the full
timeout on genuine failure. We never use fixed `sleep(30)` (slow AND still flaky).

**Takeaway:** *"Testing an async process means asserting under eventual consistency: poll
for the expected outcome with a timeout. A fixed sleep is both slower and still flaky."*

---

## 2. Proving a negative in an async system: `stays_true`

**Symptom:** how do you assert "no duplicate row is ever created" or "the message does NOT
reach the DLQ"? You can't prove a negative instantly — the bad thing might happen a second
after you checked.

**Fix:** `stays_true(condition, duration)` — assert the good state *holds* over a window,
failing the instant it breaks. (e.g. policy stays `ACTIVE` for 8s after a duplicate
message, proving the duplicate didn't corrupt it.)

**Takeaway:** *"Negative async assertions verify the bad outcome doesn't occur over a
window, not at a single instant."*

---

## 3. EventBridge has no "read the last events" API

**Symptom:** you want to assert "the `ApplicationSubmitted` event was published with the
right shape" — but EventBridge is fire-and-forget. There's nothing to query.

**Fix:** the **probe pattern** — before triggering the producer, create a temporary SQS
queue and an EventBridge rule routing the target events to it; trigger; read the queue;
assert; tear down. The probe IS a test consumer, so capturing the event also proves it was
delivered to consumers.

**Takeaway:** *"To validate EventBridge, subscribe a test consumer before triggering the
producer, then assert on what it captured — count, source, detail-type, schema, ordering."*

---

## 4. EventBridge rule propagation is not instant (the worst flake)

**Symptom:** the probe sometimes captured **0 events** even though the producer fired — but
an identical test passed. Classic heisenbug.

**Cause:** after you create an EventBridge rule, it takes a **variable** amount of time to
become active. Our first version used `sleep(3)` and hoped — sometimes the rule wasn't
routing yet when the event fired, so the event was lost to the probe.

**Fix:** **actively verify the rule is live** before returning from setup. The probe now
publishes a synthetic marker event and polls its own queue until the marker arrives —
proving the rule→queue path works — *then* lets the test proceed. Deterministic, no guessing.

**Takeaway:** *"Don't sleep-and-hope for async infrastructure to be ready — verify it.
After creating a rule/subscription, prove it's live with a round-trip before relying on it.
This eliminated our worst source of flakiness."*

---

## 5. CloudWatch logs are eventually-consistent too

**Symptom:** a monitoring test reached `ACTIVE`, then searched the billing Lambda's logs
for "First premium recorded" — and found nothing, even though billing clearly ran.

**Cause:** logs lag the actual write by a few seconds before they're searchable. Reading
once, right after the event, raced the log ingestion.

**Fix:** poll the logs with `wait_until` until **our specific** policy's log line appears —
not just until *any* matching line is there.

**Takeaway:** *"Logs are a data store with its own propagation delay. Poll for the specific
line you expect; don't read once and assume."*

---

## 6. VPC Lambdas can't reach RDS from a test runner — design around it

**Symptom:** validating that billing wrote to RDS by connecting to the database directly
from the test fails — the connection times out.

**Cause:** RDS sits in a VPC and its security group only allows the Lambda security group.
A test on your laptop (or a GitHub runner) isn't in the VPC, so it can't connect. (This is
the same VPC-reachability theme that bit the platform deploy.)

**Fix (design decision):** validate the RDS write **indirectly** via observable signals
that ARE reachable from anywhere — the policy status flipping to `ACTIVE` (which only
happens after billing succeeds) and the billing Lambda's CloudWatch log ("First premium
recorded"). The framework provides a direct `db_connection()` helper too, but the suite
doesn't depend on it — so the same tests run unchanged in CI.

**Takeaway:** *"When the data store isn't directly reachable from the test environment,
validate through observable side effects — status transitions and logs — instead of a
direct connection. It also makes the suite portable to CI with no VPC gymnastics."*

---

## 7. Make tests find the system, don't hardcode it

**Symptom:** every deploy gets a new API Gateway URL; hardcoding it means editing tests
after every deploy.

**Fix:** `framework/config.py` resolves the API URL, table names, queue URLs, and ARNs
from the deployed **CloudFormation stack outputs** at runtime. The same suite runs against
any environment by setting one env var (`VERICITY_STACK`). The same goes for Lambda
function names used in log assertions: an early version hardcoded `vericity-billing-dev`
etc., which silently searched the wrong log groups on any other stage — now
`config.lambda_function_name()` derives them from the stack's own `Stage` parameter.

**Takeaway:** *"Resolve environment specifics from infrastructure outputs at runtime so the
same suite is portable across dev/prod without edits."*

---

## 8. Clean up test data — and tolerate partial setup

**Symptom:** tests that seed DynamoDB rows or create temporary AWS resources (probe queues,
rules) leave junk behind and slowly pollute the account.

**Fix:** the `seeded_policy` fixture tracks every row it creates and deletes them on
teardown; the `EventProbe` context manager tears down its queue + rule on exit, tolerating
partial setup if `__enter__` failed midway.

**Takeaway:** *"Tests must clean up after themselves (fixtures/teardown), and teardown must
be resilient to partial setup so a failed test doesn't orphan cloud resources."*

---

## 9. Boundary testing: test BOTH sides of every limit

**Symptom:** it's tempting to test only "age 17 is rejected." But off-by-one bugs hide at
the *accepted* edge too.

**Fix:** for every limit (age 18-75, coverage $25k-$2M, term 10-30), test just-inside
(must be accepted) AND just-outside (must be rejected). Parametrized so each is its own case.

**Takeaway:** *"Boundary testing covers both sides of every limit — the inclusive edge must
pass and the just-beyond value must fail. That's where off-by-one bugs live."*

---

## 10. Slow tests are a fact of real integration testing — split them

**Symptom:** the full suite takes ~15 minutes because async propagation is genuinely slow
(the DLQ test alone waits ~3 min for a message to exhaust 3 retries at a 60s visibility
timeout).

**Fix:** mark slow tests (`@pytest.mark.slow`) and split CI — fast functional/integration
tests on every push/PR for quick feedback; the full slow suite on a schedule / manual run.

**Takeaway:** *"Real integration tests against async infra are slow by nature — you're
waiting on real retries and propagation. Separate fast feedback (unit/contract on every
commit) from slow confidence (integration/E2E on a schedule) so neither blocks the other."*

---

## 11. Test probes must not look like real traffic

**Symptom:** every events-suite run left junk behind: stray Step Functions executions
ending in ManualReview, and garbage `probe-ready-…` rows in the policies table.

**Cause:** the EventProbe's rule-is-live check (lesson 4) published its synthetic marker
events using a **real** detail-type (e.g. `ApplicationSubmitted`) on the **real** bus. The
probe's rule matched them — but so did the platform's own rules, so every marker also
started a real underwriting execution with a junk payload. (And because DynamoDB's
`update_item` upserts, the ManualReview fallback then *created* a policy row for the
marker's fake id.)

**Fix:** markers now use a QA-only detail-type (`QaProbeReady`) that no platform rule
matches; the probe's rule subscribes to it *in addition to* the real detail-types under
test. The probe also filters marker events out of what tests see, so
`wait_for_events(count=...)` counts stay accurate.

**Takeaway:** *"Synthetic test traffic on shared infrastructure must be distinguishable
from real traffic — give probes their own event type/channel so verifying your test rig
doesn't exercise (and pollute) the system under test."*

---

## The meta-lesson

Almost every gotcha here is a variant of the same truth: **distributed, asynchronous,
infrastructure-backed systems are eventually-consistent at every layer** — data, events,
logs, even the test rules you create. The whole framework is built around *verifying state
by polling with timeouts* rather than assuming anything is instant. That mindset is the
real answer to every "how would you test X in AWS?" question.
