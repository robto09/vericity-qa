"""Cached boto3 clients for the test session, all pinned to the test region."""
from __future__ import annotations

import functools
from typing import Any

import boto3

from .config import AWS_REGION


@functools.lru_cache(maxsize=None)
def client(service_name: str) -> Any:
    """Return a cached boto3 client for the given service, in the test region."""
    return boto3.client(service_name, region_name=AWS_REGION)


@functools.lru_cache(maxsize=None)
def resource(service_name: str) -> Any:
    """Return a cached boto3 resource (e.g. the high-level DynamoDB API)."""
    return boto3.resource(service_name, region_name=AWS_REGION)
