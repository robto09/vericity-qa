#!/usr/bin/env bash
#
# run-newman.sh — run the Postman collection from the CLI with Newman.
#
# Resolves the live API URL from the deployed stack and passes it to Newman, so the
# collection always targets the current deployment without manual editing.
#
# Prereq: Newman installed (`npm install -g newman`).
# Usage:  ./run-newman.sh [stack-name]

set -euo pipefail

STACK_NAME="${1:-vericity-dev}"
REGION="$(aws configure get region || echo us-east-1)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_URL="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" --output text)"

echo "Running Newman against: ${API_URL}"
newman run "${HERE}/vericity.postman_collection.json" \
  --env-var "base_url=${API_URL}" \
  --reporters cli,json \
  --reporter-json-export "${HERE}/newman-report.json"
