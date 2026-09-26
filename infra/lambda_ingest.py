"""Ring webhook edge: Lambda function URL -> verify HMAC -> SQS. Deliberately tiny and dependency-free
(boto3 is in the Lambda Python runtime). The Django worker re-validates and stores. Placeholder signature scheme:
see TODO-verify in watch/webhook.py. Env: QUEUE_URL, SECRET_ARN (Secrets Manager secret holding the shared secret)."""
import base64
import hashlib
import hmac
import json
import os
import time

_secret = None


def _get_secret(sm=None):
    global _secret
    if _secret is None:
        if sm is None:
            import boto3
            sm = boto3.client("secretsmanager")
        _secret = sm.get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
    return _secret


def handler(event, context, sqs=None, sm=None):
    """Function URL payload format 2.0: headers are lower-cased, body may be base64 (isBase64Encoded)."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    if len(raw) > 64 * 1024:
        return {"statusCode": 413, "body": "too large"}
    sig = headers.get("x-ring-signature", "")
    expected = hmac.new(_get_secret(sm).encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return {"statusCode": 403, "body": ""}
    if sqs is None:
        import boto3
        sqs = boto3.client("sqs")
    sqs.send_message(QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps({
        "type": "ring_event", "body_b64": base64.b64encode(raw).decode(), "signature": sig,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}))
    return {"statusCode": 204, "body": ""}
