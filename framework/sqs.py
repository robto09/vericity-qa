"""SQS helpers — send messages and inspect queue depth for async/DLQ testing."""
from __future__ import annotations

import json
from typing import Any

from .aws import client


def send_message(queue_url: str, body: dict[str, Any]) -> str:
    """Send a JSON message to a queue; return the message id."""
    response = client("sqs").send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
    return response["MessageId"]


def approximate_depth(queue_url: str) -> int:
    """Return the approximate number of visible messages in a queue."""
    attributes = client("sqs").get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


def purge(queue_url: str) -> None:
    """Purge all messages from a queue (used to reset DLQ state before a test).

    Note: AWS allows purge at most once every 60s per queue.
    """
    try:
        client("sqs").purge_queue(QueueUrl=queue_url)
    except Exception:  # noqa: BLE001 - purge throttle is non-fatal for tests
        pass


def receive_all(queue_url: str, max_polls: int = 3) -> list[dict[str, Any]]:
    """Drain and return message bodies currently in a queue (non-destructive-ish).

    Used to inspect what landed in the DLQ. Deletes as it reads so a second call
    doesn't re-see them.
    """
    sqs = client("sqs")
    bodies: list[dict[str, Any]] = []
    for _ in range(max_polls):
        response = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=2
        )
        messages = response.get("Messages", [])
        if not messages:
            continue
        for message in messages:
            try:
                bodies.append(json.loads(message["Body"]))
            except json.JSONDecodeError:
                bodies.append({"_raw": message["Body"]})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return bodies
