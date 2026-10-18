"""Ring webhook receiver and health endpoint."""
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import privacy, ringapi
from .models import DeviceStatus, Event, Household

log = logging.getLogger("watch.webhook")


def sign_ok(signature: str, raw: bytes) -> bool:
    """Ring signs the raw body: `X-Signature: sha256=<hex HMAC-SHA256>` (see watch/ringapi.py for the source)."""
    return ringapi.verify(signature, raw, settings.RING_WEBHOOK_SECRET)


def valid_signature(request) -> bool:
    return sign_ok(request.headers.get("X-Signature", ""), request.body)


def _reject(reason, status=400, **extra):
    log.warning("webhook rejected", extra={"reason": reason, **extra})
    return HttpResponseForbidden() if status == 403 else HttpResponseBadRequest(reason)


def ingest(raw: bytes, signature: str, now=None, check_replay=True):
    """Validate and store one Ring event. Returns (http_status, reason). Shared by the HTTP view and the SQS worker
    (which passes now=<time the edge received it> and check_replay=False, because queue delay is not a replay)."""
    if not sign_ok(signature, raw):
        return 403, "bad signature"
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 400, "invalid json"
    ev = ringapi.parse(body)
    if ev is None or not ev.account:
        return 400, "missing account"
    if not isinstance(body.get("data"), dict) and body.get("created_at") and ev.sent_at is None:
        return 400, "bad created_at"
    household = Household.objects.filter(ring_location_id=ev.account).first()
    if household is None:
        log.warning("webhook rejected", extra={"reason": "unknown account", "account": ev.account[:40]})
        return 400, "unknown location"
    now = now or timezone.now()
    window = settings.RING_REPLAY_WINDOW_SECONDS
    # Ring documents no timestamp header, so replay protection uses meta.time (when Ring sent it).
    if check_replay and window and ev.sent_at is not None and not (-300 <= (now - ev.sent_at).total_seconds() <= window):
        return 400, "stale or future timestamp"
    kind = ev.kind[:40]
    device = ev.device
    # Idempotency: Ring's data.id (<device>_<type>_<timestamp>) is unique per event, else hash of the exact body.
    ext = (ev.event_id or hashlib.sha256(raw).hexdigest())[:100]
    at = ev.at or ev.sent_at or now
    try:
        with transaction.atomic():
            Event.objects.create(household=household, kind=kind, device=device, at=at, external_id=ext,
                                 raw=privacy.sanitize_payload(ev.stored()))       # metadata only, allowlisted
            if device:
                _touch_device(household, device, at, ev)
    except IntegrityError:
        log.info("duplicate delivery ignored", extra={"household": household.pk})   # acknowledge, do not double-record
        return 204, "duplicate"
    log.info("event stored", extra={"household": household.pk, "kind": kind})
    return 204, "stored"


@csrf_exempt
@require_POST
def ring_webhook(request):
    code, reason = ingest(request.body, request.headers.get("X-Signature", ""))
    if code == 204:
        return HttpResponse(status=204)
    return _reject(reason, 403 if code == 403 else 400)


def _touch_device(h, device, at, ev):
    d, _ = DeviceStatus.objects.get_or_create(household=h, device=device, defaults={"last_seen": at})
    if at >= d.last_seen:
        d.last_seen = at
    # Ring sends no battery webhook: battery comes from polling /v1/devices/{id}/status (see poll_ring_status).
    # The legacy flat format may still carry `battery`/`online`.
    b = (ev.extra or {}).get("battery")
    if isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 100:
        d.battery = b
    d.online = False if (ev.kind == "offline" or (ev.extra or {}).get("online") is False) else True
    d.save()


@require_GET
def healthz(request):
    """200 if the app and DB are up. ?deep=1 also pings the Celery broker."""
    out = {"status": "ok", "time": timezone.now().isoformat()}
    code = 200
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
        last = Event.objects.order_by("-at").values_list("at", flat=True).first()
        out["last_event_at"] = last.isoformat() if last else None
    except Exception as e:  # noqa
        out.update(status="error", db=str(e)[:100])
        code = 503
    if request.GET.get("deep"):
        try:
            import redis
            redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1).ping()
            out["broker"] = "ok"
        except Exception as e:  # noqa
            out.update(broker="unreachable", status="degraded")
            code = 503
    return JsonResponse(out, status=code)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Telegram calls this with the secret_token given to setWebhook in X-Telegram-Bot-Api-Secret-Token.
    Fails closed: with no secret configured, every request is rejected."""
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(got.encode(), secret.encode()):
        return _reject("bad telegram secret", 403)
    try:
        update = json.loads(request.body)
    except ValueError:
        return _reject("bad json")
    if isinstance(update, dict):
        from . import tgbot
        tgbot.handle_update(update)
    return HttpResponse("ok")   # always 200 once authenticated, so Telegram does not redeliver
