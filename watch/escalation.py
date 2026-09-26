"""Escalation chain: gentle nudge to the person, then caregiver 1, caregiver 2 ..., then emergency contacts.
Acknowledgement at any point stops the chain. Every message is audited as a Notification row."""
import logging
from datetime import timedelta
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from . import notify
from .models import CheckIn, Membership
from .notify import Target

log = logging.getLogger("watch.escalation")
URGENT_FROM_STEP = 2   # from the 3rd step on, caregivers' quiet hours no longer hold back phone alerts


@dataclass
class Step:
    kind: str
    label: str
    targets: list = field(default_factory=list)


def _member_target(m):
    p = getattr(m.user, "profile", None)
    return Target(m.user.get_full_name() or m.user.username, "caregiver", email=m.user.email,
                  phone=getattr(p, "phone", ""), user=m.user, channels=notify.channels_for_member(m), membership=m)


def build_chain(h, kind=CheckIn.QUIET):
    members = list(h.memberships.filter(role__in=[Membership.OWNER, Membership.CAREGIVER])
                   .select_related("user").order_by("priority", "id"))
    if kind == CheckIn.SENSOR:
        owners = [m for m in members if m.role == Membership.OWNER]
        first = members[:1]
        seen, targets = set(), []
        for m in owners + first:
            if m.pk not in seen:
                seen.add(m.pk)
                targets.append(_member_target(m))
        return [Step("caregiver", "Device notice", targets)] if targets else []
    chain = []
    if h.nudge_enabled and h.person_phone:
        chs = ["sms"] + (["whatsapp"] if settings.WHATSAPP_ENABLED else [])
        chain.append(Step("nudge", h.name, [Target(h.name, "nudge", phone=h.person_phone, channels=chs)]))
    for m in members:
        t = _member_target(m)
        chain.append(Step("caregiver", t.label, [t]))
    contacts = []
    for c in h.emergency_contacts.all():
        chs = (["email"] if c.email else []) + (["sms"] if c.phone else []) + (["whatsapp"] if c.phone and settings.WHATSAPP_ENABLED else [])
        contacts.append(Target(c.name, "emergency", email=c.email, phone=c.phone, contact=c, channels=chs))
    if contacts:
        chain.append(Step("emergency", "Emergency contacts", contacts))
    return chain


def _delta(h, step):
    return timedelta(minutes=min(10, h.escalation_minutes) if step.kind == "nudge" else h.escalation_minutes)


def _run_step(checkin, idx, step, now, urgent=False):
    rows = []
    for t in step.targets:
        rows += notify.deliver(checkin, idx, t, now, urgent=urgent or idx >= URGENT_FROM_STEP or step.kind == "emergency")
    handled = any(r.status in notify.PROGRESS for r in rows)
    log.info("escalation step", extra={"checkin": checkin.pk, "step": idx, "kind": step.kind, "handled": handled})
    return handled


def advance(checkin: CheckIn, now=None) -> int:
    """Send whatever steps are due. Returns number of steps run."""
    now = now or timezone.now()
    if checkin.state != CheckIn.OPEN:
        return 0
    chain = build_chain(checkin.household, checkin.kind)
    ran = 0
    while checkin.level < len(chain) and (checkin.next_step_at is None or now >= checkin.next_step_at):
        step = chain[checkin.level]
        handled = _run_step(checkin, checkin.level, step, now)
        checkin.level += 1
        ran += 1
        checkin.next_step_at = _delta_at(now, checkin.household, step) if handled else None
    if checkin.level >= len(chain) and (checkin.next_step_at is None or now >= checkin.next_step_at):
        checkin.state = CheckIn.EXHAUSTED
        log.error("escalation exhausted without acknowledgement", extra={"checkin": checkin.pk})
    checkin.save()
    return ran


def _delta_at(now, h, step):
    return now + _delta(h, step)


def start(h, assessment, now=None, kind=CheckIn.QUIET):
    """Open today's check-in (once per kind per day) and send the first step."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    checkin, created = CheckIn.objects.get_or_create(
        household=h, date=today, kind=kind,
        defaults=dict(confidence=assessment.confidence, explanation=assessment.explanation[:300]))
    if not created:
        return None
    advance(checkin, now)
    return checkin


def acknowledge(checkin: CheckIn, resolution="all_ok", user=None, name="", now=None):
    """all_ok / person_ok close the chain; needs_help immediately alerts every remaining step as urgent."""
    now = now or timezone.now()
    who = name or (user.get_full_name() or user.username if user else "")
    if checkin.state in (CheckIn.ACK,) and resolution != "needs_help":
        return checkin
    prior = set(checkin.notifications.exclude(user=None).values_list("user_id", flat=True))
    checkin.resolution, checkin.resolved_at, checkin.acknowledged_by = resolution, now, who
    if resolution == "needs_help":
        checkin.state = CheckIn.HELP
        chain = build_chain(checkin.household, checkin.kind)
        for idx in range(checkin.level, len(chain)):
            _run_step(checkin, idx, chain[idx], now, urgent=True)
        checkin.level, checkin.next_step_at = len(chain), None
    else:
        checkin.state, checkin.next_step_at = CheckIn.ACK, None
    checkin.save()
    _inform(checkin, prior - ({user.pk} if user else set()), who, resolution, now)
    return checkin


def acknowledge_by_token(token, resolution="all_ok", now=None):
    from .models import Notification
    n = Notification.objects.select_related("checkin__household").filter(ack_token=token).first()
    if not n:
        return None
    return acknowledge(n.checkin, resolution, user=n.user, name=n.recipient, now=now)


def _inform(checkin, prior_user_ids, who, resolution, now):
    """Tell caregivers who were already messaged that someone has it, so nobody double-calls."""
    from .models import Notification
    if checkin.kind == CheckIn.SENSOR:
        return
    h = checkin.household
    text = (f"{who or 'Someone'} says {h.name} needs help." if resolution == "needs_help"
            else f"{who or 'Someone'} confirmed {h.name} is OK. No further action needed.")
    for m in h.memberships.filter(user_id__in=prior_user_ids).select_related("user"):
        if m.user.email:
            n = Notification.objects.create(checkin=checkin, step=99, step_kind="info", recipient=str(m.user),
                                            destination=notify.channels.mask(m.user.email), channel="email",
                                            user=m.user, text=text, created_at=now)
            try:
                notify.channels.email(m.user.email, f"Update: {h.name}", text)
                n.status, n.detail = "sent", "follow-up so nobody double-calls"
            except Exception as e:  # noqa
                n.status, n.detail = "failed", str(e)[:200]
            n.save()
