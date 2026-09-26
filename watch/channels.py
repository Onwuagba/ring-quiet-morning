"""Delivery channels. Each returns Result(status, detail): sent | stubbed (not configured) | error.
Twilio SMS/WhatsApp and web push are config-flagged and are stubs (logged only) without keys."""
import json
import logging
from collections import namedtuple

from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger("watch.channels")
Result = namedtuple("Result", "status detail")


def mask(dest: str) -> str:
    if "@" in dest:
        u, d = dest.split("@", 1)
        return (u[:1] + "***@" + d) if u else dest
    return ("*" * max(0, len(dest) - 4)) + dest[-4:] if dest else ""


def email(to, subject, body, **_):
    if not to:
        return Result("error", "no email address")
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
    return Result("sent", "email handed to backend " + settings.EMAIL_BACKEND.rsplit(".", 2)[-2])


def _twilio(to, body, sender, prefix, flag):
    if not flag:
        return Result("stubbed", "channel disabled by config flag")
    sid, tok = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
    if not (sid and tok and sender):
        log.info("twilio stub", extra={"channel": prefix or "sms", "to": mask(to)})
        return Result("stubbed", "Twilio keys not set: logged only")
    if not to:
        return Result("error", "no phone number")
    import requests
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok), timeout=10,
                      data={"From": f"{prefix}{sender}", "To": f"{prefix}{to}", "Body": body})
    return Result("sent", f"twilio {r.status_code}") if r.ok else Result("error", f"twilio {r.status_code}: {r.text[:120]}")


def sms(to, body, **_):
    return _twilio(to, body, settings.TWILIO_SMS_FROM, "", settings.SMS_ENABLED)


def whatsapp(to, body, **_):
    # TODO-verify: WhatsApp business-initiated messages need pre-approved templates outside the 24h session window.
    return _twilio(to, body, settings.TWILIO_WHATSAPP_FROM, "whatsapp:", settings.WHATSAPP_ENABLED)


def push(user, title, body, url="/", **_):
    """Web Push (VAPID). Needs HTTPS (localhost is exempt) and a service worker; on iOS only for installed PWAs (16.4+, unverified here)."""
    subs = list(user.push_subscriptions.all()) if user else []
    if not subs:
        return Result("error", "no push subscription on this account")
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        log.info("push stub", extra={"user": getattr(user, "pk", None)})
        return Result("stubbed", "VAPID keys not set: logged only")
    from pywebpush import WebPushException, webpush
    ok = 0
    last = ""
    for s in subs:
        try:
            webpush({"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    json.dumps({"title": title, "body": body, "url": url}),
                    vapid_private_key=settings.VAPID_PRIVATE_KEY, vapid_claims={"sub": settings.VAPID_CLAIM})
            ok += 1
        except WebPushException as e:
            last = str(e)[:120]
            if getattr(e.response, "status_code", 0) in (404, 410):
                s.delete()  # subscription expired
    return Result("sent", f"push to {ok}/{len(subs)} devices") if ok else Result("error", last or "push failed")


def console(target, body, **_):
    log.warning("check-in message", extra={"to": target, "body": body[:200]})
    return Result("sent", "logged")
