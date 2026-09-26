"""Ring webhook receiver and health endpoint."""
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import privacy
from .models import DeviceStatus, Event, Household

log = logging.getLogger("watch.webhook")


def valid_signature(request) -> bool:
    # TODO-verify: PLACEHOLDER scheme. The real Ring webhook signature header/algorithm is unknown to us;
    # confirm in the Ring developer docs and replace. Do not treat this as Ring's actual scheme.
    sig = request.headers.get("X-Ring-Signature", "")
    expected = hmac.new(settings.RING_WEBHOOK_SECRET.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _reject(reason, status=400, **extra):
    log.warning("webhook rejected", extra={"reason": reason, **extra})
    return HttpResponseForbidden() if status == 403 else HttpResponseBadRequest(reason)


@csrf_exempt
@require_POST
def ring_webhook(request):
    if not valid_signature(request):
        return _reject("bad signature", 403)
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return _reject("invalid json")
    if not isinstance(body, dict) or not isinstance(body.get("location_id"), str):
        return _reject("missing location_id")
    household = Household.objects.filter(ring_location_id=body["location_id"]).first()
    if household is None:
        return _reject("unknown location", location=body["location_id"][:40])
    now = timezone.now()
    when = None
    if body.get("created_at"):
        try:
            when = parse_datetime(str(body["created_at"]))
        except ValueError:
            when = None
        if when is None:
            return _reject("bad created_at")
        if timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.utc)
    window = settings.RING_REPLAY_WINDOW_SECONDS
    if window and when is not None and not (-300 <= (now - when).total_seconds() <= window):
        return _reject("stale or future timestamp", household=household.pk)   # replay protection
    kind = str(body.get("event_type", "unknown"))[:40]
    device = str(body.get("device_id", ""))[:100]
    # Idempotency: prefer Ring's event id (TODO-verify field name), else hash of the exact body.
    ext = str(body.get("event_id") or body.get("id") or hashlib.sha256(request.body).hexdigest())[:100]
    at = when or now
    try:
        with transaction.atomic():
            Event.objects.create(household=household, kind=kind, device=device, at=at, external_id=ext,
                                 raw=privacy.sanitize_payload(body))       # metadata only, allowlisted
            if device:
                _touch_device(household, device, at, body, kind)
    except IntegrityError:
        log.info("duplicate delivery ignored", extra={"household": household.pk})   # acknowledge, do not double-record
        return HttpResponse(status=204)
    log.info("event stored", extra={"household": household.pk, "kind": kind})
    return HttpResponse(status=204)


def _touch_device(h, device, at, body, kind):
    d, _ = DeviceStatus.objects.get_or_create(household=h, device=device, defaults={"last_seen": at})
    if at >= d.last_seen:
        d.last_seen = at
    # TODO-verify: Ring may not send battery/online signals at all; these field names are our own generic schema.
    b = body.get("battery")
    if isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 100:
        d.battery = b
    d.online = False if (kind == "offline" or body.get("online") is False) else True
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
