"""Delivery channels. Each returns Result(status, detail): sent | stubbed (not configured) | error.
Telegram and web push are stubs (logged only) without keys."""
import json
import logging
from collections import namedtuple

from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger("watch.channels")
Result = namedtuple("Result", "status detail extra", defaults=(None,))


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


def telegram(chat_id, body, ack_token=None, **_):
    """Send via the Telegram Bot API with 'All OK' / 'Need help' buttons. Result.extra carries the message id."""
    from . import telegram as tg
    if not chat_id:
        return Result("error", "no telegram chat linked")
    if not tg.configured():
        log.info("telegram stub", extra={"channel": "telegram"})
        return Result("stubbed", "Telegram bot token not set: logged only")
    try:
        msg = tg.send_message(chat_id, body, tg.keyboard(ack_token) if ack_token else None)
    except tg.TelegramError as e:
        why = f"telegram {e.code}: {e}"[:150] + (f" (retry after {e.retry_after}s)" if e.retry_after else "")
        return Result("error", why)
    return Result("sent", "telegram delivered", (msg or {}).get("message_id"))


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
