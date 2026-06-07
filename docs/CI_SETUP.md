# CI Setup — making the QA suite run in GitHub Actions

**Honest status:** the workflow file (`.github/workflows/qa.yml`) is ready, but CI won't
actually run until you do three setup steps below. None require code changes, and **no
other repo needs modifying** — the QA repo is self-contained.

---

## Why these steps are needed (the gaps)

These tests hit the **live, deployed AWS stack**, not mocks. So CI needs: (1) to exist on
GitHub, (2) AWS credentials, and (3) the stack to be up when it runs. The workflow already
handles the trickier parts for you:

- It runs a **preflight** (`python -m framework.preflight`) that fails with a clear message
  if the stack isn't deployed, RDS is stopped, or the API is down — so you get one
  actionable error instead of a wall of confusing test failures.
- It splits **fast tests** (every push/PR) from **slow tests** (schedule/manual), so PR
  feedback stays quick.
- The suite validates billing via **policy status + CloudWatch logs**, NOT a direct RDS
  connection — so it works from a plain GitHub runner with no VPC setup. (A direct DB
  connection would fail from outside the VPC; we designed around that.)

---

## Step 1 — Put the repo on GitHub

CI can't trigger without a remote.

```bash
cd vericity-qa
git init
git add .
git commit -m "Vericity QA framework"
# Create an empty repo on github.com, then:
git remote add origin https://github.com/<you>/vericity-qa.git
git branch -M main
git push -u origin main
```

> If you keep the platform + QA in one repo instead, move `qa.yml` to the repo root's
> `.github/workflows/` and adjust the `working-directory`/paths accordingly.

## Step 2 — Add AWS credentials as GitHub secrets

The workflow references `secrets.AWS_ACCESS_KEY_ID` and `secrets.AWS_SECRET_ACCESS_KEY`.
Create them:

1. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**.
2. Add **`AWS_ACCESS_KEY_ID`** and **`AWS_SECRET_ACCESS_KEY`**.
   - Use a **dedicated, least-privilege IAM user for CI** — ideally read-mostly plus the
     specific actions the tests need (CloudFormation describe; DynamoDB read/write to the
     test tables; SQS send/receive/create for the probe; EventBridge put/rule for the
     probe; Step Functions start/describe; S3 get/put on the docs bucket; Logs filter;
     RDS describe; Secrets Manager get). Don't reuse your personal admin keys.

> **Even better (no long-lived keys): OIDC.** Replace the access-key step with GitHub's
> OIDC + an IAM role (`aws-actions/configure-aws-credentials` supports `role-to-assume`).
> That avoids storing static secrets at all. Optional; the secret-based path works fine to
> start.

## Step 3 — Make sure the stack is up when CI runs

The tests need the platform stack **deployed** and **RDS started**.

- **For the daily scheduled run:** RDS must be running at 06:00 UTC (the cron time). If you
  stop RDS overnight to save cost, either (a) change the cron to a time you'll have it
  running, or (b) add a step to the workflow that starts RDS before the tests and stops it
  after (see snippet below).
- **For push/PR runs:** the fast tests (`-m "not slow"`) avoid the billing→RDS path, so
  they pass even if RDS is stopped — but the preflight currently requires RDS available.
  If you want PR runs to work with RDS stopped, relax the preflight (see note below).

### Optional: start/stop RDS around the scheduled full run

Add these steps to the `full-tests` job to control cost (RDS only runs during the test
window):

```yaml
      - name: Start RDS
        run: |
          aws rds start-db-instance --db-instance-identifier vericity-billing-dev || true
          aws rds wait db-instance-available --db-instance-identifier vericity-billing-dev
      # ... preflight + tests ...
      - name: Stop RDS (always, even on failure)
        if: always()
        run: aws rds stop-db-instance --db-instance-identifier vericity-billing-dev || true
```

### Note: making PR runs tolerate stopped RDS

If you want fast PR runs while RDS is stopped, change the preflight call in the
`fast-tests` job to skip the RDS check, or run `pytest -m "not slow"` without preflight
(the fast tests don't touch RDS). The slow/full suite should always preflight RDS.

---

## Verify locally before pushing

```bash
# Same preflight CI runs:
.venv/bin/python -m framework.preflight

# Same fast-test command CI runs on every push/PR:
.venv/bin/pytest -m "not slow"
```

If those pass locally with your AWS creds, CI will pass once the secrets are set and the
stack is up.

---

## What CI gives you (the "quality gate" story)

- **Every push/PR:** fast functional + integration + contract tests run automatically; a
  regression fails the check before merge. This is the "CI/CD pipelines and quality gates"
  requirement, made concrete.
- **Daily + on-demand:** the full suite (events, workflow, async/DLQ, pipeline, monitoring)
  runs against real AWS, catching async/integration regressions a unit suite never could.
- **HTML reports** are uploaded as artifacts on every run for handoff/review.
