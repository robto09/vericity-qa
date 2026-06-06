"""JSON Schemas for API responses and EventBridge event details.

Contract testing: instead of asserting individual fields one-by-one in every test, we
validate payloads against a declared schema. This catches missing fields, wrong types,
and shape drift in one assertion — and documents the contract each producer must honor.
"""
from __future__ import annotations

# --- API response schemas ---
QUOTE_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["quote_id", "monthly_premium", "annual_premium", "coverage_amount", "status"],
    "properties": {
        "quote_id": {"type": "string", "pattern": "^qte_"},
        "monthly_premium": {"type": "number", "minimum": 0},
        "annual_premium": {"type": "number", "minimum": 0},
        "coverage_amount": {"type": "integer", "minimum": 0},
        "term_years": {"type": "integer"},
        "status": {"const": "QUOTED"},
    },
}

APPLICATION_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["policy_id", "status", "quoted_monthly_premium", "quoted_annual_premium"],
    "properties": {
        "policy_id": {"type": "string", "pattern": "^pol_"},
        "status": {"const": "APPLIED"},
        "quoted_monthly_premium": {"type": "number", "minimum": 0},
        "quoted_annual_premium": {"type": "number", "minimum": 0},
    },
}

# --- EventBridge event detail schemas (the `detail` payload, post-unwrap) ---
APPLICATION_SUBMITTED_DETAIL_SCHEMA = {
    "type": "object",
    "required": ["policy_id", "applicant_age", "coverage_amount", "term_years"],
    "properties": {
        "policy_id": {"type": "string", "pattern": "^pol_"},
        "applicant_age": {"type": "integer", "minimum": 18, "maximum": 75},
        "coverage_amount": {"type": "integer"},
        "term_years": {"type": "integer"},
        "tobacco_use": {"type": "boolean"},
    },
}

POLICY_APPROVED_DETAIL_SCHEMA = {
    "type": "object",
    "required": ["policy_id", "risk_tier", "final_annual_premium", "final_monthly_premium"],
    "properties": {
        "policy_id": {"type": "string", "pattern": "^pol_"},
        "risk_tier": {"enum": ["PREFERRED", "STANDARD", "SUBSTANDARD"]},
        # Premiums are serialized as strings in events (Decimal -> str).
        "final_annual_premium": {"type": "string"},
        "final_monthly_premium": {"type": "string"},
    },
}

POLICY_ISSUED_DETAIL_SCHEMA = {
    "type": "object",
    "required": ["policy_id", "final_monthly_premium", "document_s3_key"],
    "properties": {
        "policy_id": {"type": "string", "pattern": "^pol_"},
        "final_monthly_premium": {"type": "string"},
        "document_s3_key": {"type": "string", "pattern": "^policies/"},
    },
}
