"""Thin, fail-safe wrapper around Amazon Bedrock Converse (bedrock-runtime).

Contract: `generate()` returns text or None. None means "use the deterministic fallback" and is returned when the
feature is off, the daily cap is reached, the call errors or times out, or a guardrail intervened. It never raises.
Request/response shape follows the Converse docs (modelId, system, messages, inferenceConfig, guardrailConfig;
response output.message.content[].text, stopReason, usage). See docs/AWS_STACK.md. Not verified against live AWS."""
import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

log = logging.getLogger("watch.bedrock")
_client = None
MAX_PROMPT_CHARS = 4000


def enabled() -> bool:
    return bool(settings.BEDROCK_MODEL_ID)


def get_client():
    """Lazily build a boto3 bedrock-runtime client (boto3 is only needed when this is switched on)."""
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client("bedrock-runtime", region_name=settings.AWS_REGION, config=Config(
            read_timeout=settings.BEDROCK_TIMEOUT_SECONDS, connect_timeout=3, retries={"max_attempts": 2}))
    return _client


def _reserve_call() -> bool:
    """Atomically count a call against today's cap. False = cap reached."""
    from .. import models
    today = timezone.localdate()
    models.AiUsage.objects.get_or_create(date=today)
    cap = settings.BEDROCK_DAILY_CALL_CAP
    n = models.AiUsage.objects.filter(date=today, calls__lt=cap).update(calls=F("calls") + 1)
    return bool(n)


def _record_tokens(usage):
    from .. import models
    try:
        models.AiUsage.objects.filter(date=timezone.localdate()).update(
            input_tokens=F("input_tokens") + int(usage.get("inputTokens", 0)),
            output_tokens=F("output_tokens") + int(usage.get("outputTokens", 0)))
    except Exception:  # noqa - accounting must never break delivery
        log.warning("usage accounting failed")


def generate(system: str, prompt: str, max_tokens=None, temperature=0.3, client=None):
    if not enabled():
        return None
    if not _reserve_call():
        log.warning("bedrock daily call cap reached; using fallback")
        return None
    req = dict(modelId=settings.BEDROCK_MODEL_ID,
               system=[{"text": system}],
               messages=[{"role": "user", "content": [{"text": prompt[:MAX_PROMPT_CHARS]}]}],
               inferenceConfig={"maxTokens": max_tokens or settings.BEDROCK_MAX_OUTPUT_TOKENS, "temperature": temperature})
    if settings.BEDROCK_GUARDRAIL_ID and settings.BEDROCK_GUARDRAIL_VERSION:
        req["guardrailConfig"] = {"guardrailIdentifier": settings.BEDROCK_GUARDRAIL_ID,
                                  "guardrailVersion": settings.BEDROCK_GUARDRAIL_VERSION}
    try:
        resp = (client or get_client()).converse(**req)
    except Exception as e:  # noqa - ClientError, timeouts, missing boto3/credentials: all mean "fall back"
        log.warning("bedrock call failed", extra={"error": type(e).__name__})
        return None
    _record_tokens(resp.get("usage") or {})
    if resp.get("stopReason") != "end_turn":     # guardrail_intervened, max_tokens, content_filtered ... : do not trust
        log.info("bedrock output not used", extra={"stop": resp.get("stopReason")})
        return None
    try:
        parts = [b["text"] for b in resp["output"]["message"]["content"] if "text" in b]
    except (KeyError, TypeError):
        return None
    text = " ".join(parts).strip()
    return text or None
