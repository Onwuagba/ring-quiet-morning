"""Message composition and audited delivery (per person, per channel) with quiet hours and retry/backoff."""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from . import channels
from .models import Membership, Notification

log = logging.getLogger("watch.notify")
BACKOFF_MINUTES = (1, 5, 15)     # retry delays after failure 1, 2, 3
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
PROGRESS = ("sent", "stubbed", "retrying", "deferred")   # statuses that count as "we tried to reach them"


@dataclass
class Target:
    label: str
    kind: str                        # nudge / caregiver / emergency
    email: str = ""
    phone: str = ""
    user: object = None
    contact: object = None
    channels: list = field(default_factory=list)
    membership: object = None


def site(path):
    return settings.SITE_URL.rstrip("/") + path


def ack_url(n):
    return site(reverse("ack", args=[n.ack_token]))


def compose(checkin, target, n):
    h = checkin.household
    url = ack_url(n)
    if checkin.kind == "sensor":
        return (f"Device check for {h.name}'s home: {checkin.explanation} This is about the device, "
                f"not necessarily about {h.name}. Details and confirm: {url}")
    if target.kind == "nudge":
        return (f"Good morning {h.name}. Just a quick hello from Quiet Morning: are you OK? "
                f"Tap here to let your family know you're fine: {url}")
    if target.kind == "emergency":
        return (f"You are listed as an emergency contact for {h.name}. This morning has been quiet at their home "
                f"and their caregivers have not yet confirmed they are OK. Please try to reach them. "
                f"Not an emergency call, just a request. Confirm here: {url}")
    if checkin.state == "help_requested":
        return f"A caregiver says {h.name} may need help. Please call them now. Details: {url}"
    return (f"Quiet morning at {h.name}'s home. {checkin.explanation} (Confidence {checkin.confidence}%.) "
            f"This is a gentle check-in, not an emergency. Please call them, then confirm: {url}")


def in_quiet_hours(m, now) -> bool:
    if not (m and m.quiet_start and m.quiet_end):
        return False
    t = timezone.localtime(now).time()
    s, e = m.quiet_start, m.quiet_end
    return (s <= t < e) if s <= e else (t >= s or t < e)


def channels_for_member(m: Membership):
    return [c for c, on in (("email", m.notify_email), ("push", m.notify_push), ("sms", m.notify_sms),
                            ("whatsapp", m.notify_whatsapp)) if on]


def deliver(checkin, step, target: Target, now, urgent=False):
    """Send to one target over each of its channels. Returns the Notification rows (the audit trail)."""
    quiet = not urgent and in_quiet_hours(target.membership, now)
    rows = []
    dest = {"email": target.email, "sms": target.phone, "whatsapp": target.phone,
            "push": "web push"}
    for ch in target.channels:
        n = Notification.objects.create(checkin=checkin, step=step, step_kind=target.kind, recipient=target.label,
                                        destination=channels.mask(dest.get(ch, "")) if ch != "push" else "web push",
                                        channel=ch, user=target.user, contact=target.contact, created_at=now)
        n.text = compose(checkin, target, n)
        if quiet and ch != "email":
            n.status, n.detail = "deferred", (f"Quiet hours {target.membership.quiet_start:%H:%M}-"
                                              f"{target.membership.quiet_end:%H:%M}: not sent by {ch} (email only) unless it becomes urgent")
        else:
            attempt(n, target, now)
        n.save()
        rows.append(n)
    if not rows:
        n = Notification.objects.create(checkin=checkin, step=step, step_kind=target.kind, recipient=target.label,
                                        channel="none", status="skipped", detail="No channel enabled or available",
                                        created_at=now)
        rows.append(n)
    return rows


def attempt(n: Notification, target: Target, now):
    """One send attempt with retry bookkeeping. Never raises."""
    h = n.checkin.household
    subject = (f"Device check: {h.name}" if n.checkin.kind == "sensor" else f"Quiet morning check-in: {h.name}")
    n.attempts += 1
    try:
        if n.channel == "email":
            r = channels.email(target.email, subject, n.text)
        elif n.channel == "sms":
            r = channels.sms(target.phone, n.text)
        elif n.channel == "whatsapp":
            r = channels.whatsapp(target.phone, n.text)
        elif n.channel == "push":
            r = channels.push(target.user, subject, n.text[:140], url=ack_url(n))
            if r.status == "error" and "no push subscription" in r.detail:
                n.status, n.detail, n.next_retry_at = "skipped", r.detail, None
                return
        else:
            r = channels.Result("error", "unknown channel")
    except Exception as e:  # a broken channel must never break the others
        log.exception("channel failed", extra={"channel": n.channel})
        r = channels.Result("error", f"{type(e).__name__}: {e}"[:250])
    if r.status in ("sent", "stubbed"):
        n.status, n.detail, n.next_retry_at = r.status, r.detail, None
    elif n.attempts < MAX_ATTEMPTS:
        n.status, n.detail = "retrying", r.detail
        n.next_retry_at = now + timedelta(minutes=BACKOFF_MINUTES[n.attempts - 1])
    else:
        n.status, n.detail, n.next_retry_at = "failed", f"gave up after {n.attempts} attempts: {r.detail}", None
        log.error("notification failed permanently", extra={"notification": n.pk})


def target_for(n: Notification):
    h = n.checkin.household
    if n.user_id:
        m = Membership.objects.filter(user=n.user, household=h).first()
        p = getattr(n.user, "profile", None)
        return Target(n.recipient, "caregiver", email=n.user.email, phone=getattr(p, "phone", ""), user=n.user, membership=m)
    if n.contact_id:
        return Target(n.recipient, "emergency", email=n.contact.email, phone=n.contact.phone, contact=n.contact)
    return Target(n.recipient, "nudge", phone=h.person_phone)


def retry_due(now=None) -> int:
    """Re-attempt notifications whose backoff has elapsed, unless the check-in was closed meanwhile."""
    now = now or timezone.now()
    done = 0
    for n in Notification.objects.filter(status="retrying", next_retry_at__lte=now).select_related("checkin"):
        if n.checkin.state not in ("open", "help_requested"):
            n.status, n.detail, n.next_retry_at = "skipped", "check-in already acknowledged", None
        else:
            attempt(n, target_for(n), now)
        n.save()
        done += 1
    return done
