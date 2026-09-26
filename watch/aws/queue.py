"""SQS-driven worker: the AWS-native alternative to Celery + Redis.

Messages are JSON. Producers: EventBridge Scheduler (periodic jobs) and the ingest Lambda (Ring webhooks).
  {"type": "tick"} | {"type": "purge"} | {"type": "weekly_summary"}
  {"type": "ring_event", "body_b64": "...", "signature": "...", "received_at": "<iso8601>"}
Handlers are idempotent (ticks are safe to repeat; events dedupe on external_id), which matters because SQS
standard queues are at-least-once. A message is deleted only after it was handled; failures are left for SQS
redelivery and, after maxReceiveCount, the dead-letter queue (configured in infra/quiet-morning.yml).
The boto3 calls (receive_message / delete_message) follow the SQS API; not verified against live AWS."""
import base64
import json
import logging

from django.conf import settings
from django.utils.dateparse import parse_datetime

log = logging.getLogger("watch.queue")


def get_client():
    import boto3
    return boto3.client("sqs", region_name=settings.AWS_REGION)


def handle(msg: dict) -> str:
    """Run one decoded message. Returns a short result string; raises on retryable failure."""
    kind = msg.get("type")
    if kind == "tick":
        from .. import tasks
        return f"tick {tasks.tick()}"
    if kind == "purge":
        from .. import tasks
        return f"purge {tasks.purge_old_data()}"
    if kind == "weekly_summary":
        from .. import tasks
        return f"weekly {tasks.send_weekly_summaries()}"
    if kind == "ring_event":
        from .. import webhook
        raw = base64.b64decode(msg["body_b64"])
        received = parse_datetime(msg.get("received_at") or "")
        code, reason = webhook.ingest(raw, msg.get("signature", ""), now=received, check_replay=False)
        if code == 403 or code == 400:
            log.warning("queued ring event dropped", extra={"reason": reason})   # permanent: do not retry forever
        return f"ring_event {code} {reason}"
    log.warning("unknown queue message type", extra={"type": str(kind)[:40]})
    return "ignored"


def poll_once(client=None, queue_url=None, wait=20, max_messages=10) -> int:
    """Receive up to max_messages, handle, delete the ones that succeeded. Returns number handled."""
    client = client or get_client()
    url = queue_url or settings.SQS_QUEUE_URL
    resp = client.receive_message(QueueUrl=url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait)
    done = 0
    for m in resp.get("Messages", []):
        try:
            msg = json.loads(m["Body"])
            log.info("queue message handled", extra={"result": handle(msg)})
        except (ValueError, KeyError, TypeError):
            log.warning("poison message dropped")                    # undecodable: retrying cannot help
        except Exception:  # noqa
            log.exception("queue message failed; leaving for redelivery")
            continue
        client.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
        done += 1
    return done
