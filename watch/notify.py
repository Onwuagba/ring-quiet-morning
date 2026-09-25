"""Notification channels. Default: console + email. WhatsApp is opt-in and stubbed without keys."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

log = logging.getLogger(__name__)


def confirm_url(household) -> str:
    return settings.SITE_URL.rstrip("/") + reverse("dashboard", args=[household.access_token])


def message_for(household) -> str:
    return (f"Quiet morning at {household.name}'s home: no door or motion activity yet today. "
            f"This is a gentle check-in, not an emergency. Please call them, then tap All OK: {confirm_url(household)}")


def _console(household, checkin, text):
    log.warning("CHECK-IN [%s] -> %s: %s", household.name, household.caregiver_contact, text)
    return True


def _email(household, checkin, text):
    if not household.caregiver_email:
        return False
    send_mail(f"Quiet morning check-in: {household.name}", text, settings.DEFAULT_FROM_EMAIL,
              [household.caregiver_email])
    return True


def _whatsapp(household, checkin, text):
    """Stub. Real send only if WHATSAPP_ENABLED and Twilio keys are set. TODO-verify: template approval rules."""
    if not settings.WHATSAPP_ENABLED:
        return False
    sid, tok, sender = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_WHATSAPP_FROM
    if not (sid and tok and sender):
        log.info("WhatsApp enabled but no keys; stub only: %s", text)
        return False
    import requests  # optional dependency, only needed here
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok),
                      data={"From": f"whatsapp:{sender}", "To": f"whatsapp:{household.caregiver_contact}",
                            "Body": text}, timeout=10)
    return r.ok


CHANNELS = {"console": _console, "email": _email, "whatsapp": _whatsapp}


def send_check_in(household, checkin):
    text = message_for(household)
    delivered = []
    for name in settings.NOTIFY_CHANNELS:
        fn = CHANNELS.get(name)
        try:
            if fn and fn(household, checkin, text):
                delivered.append(name)
        except Exception:  # one broken channel must not block the others
            log.exception("channel %s failed", name)
    return delivered
