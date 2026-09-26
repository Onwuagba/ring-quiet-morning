"""Escalation chain: gentle nudge to the person, then caregiver 1, caregiver 2 ..., then emergency contacts.
Acknowledgement at any point stops the chain. Every message is audited as a Notification row."""
import logging
from datetime import timedelta
from dataclasses import dataclass, field

from django.db import transaction
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
                  chat_id=getattr(p, "telegram_chat_id", None), user=m.user, channels=notify.channels_for_member(m), membership=m)


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
    if h.nudge_enabled and h.person_telegram_chat_id:
        chain.append(Step("nudge", h.name, [Target(h.name, "nudge", chat_id=h.person_telegram_chat_id, channels=["telegram"])]))
    for m in members:
        t = _member_target(m)
        chain.append(Step("caregiver", t.label, [t]))
    contacts = []
    for c in h.emergency_contacts.all():
        chs = (["email"] if c.email else []) + (["telegram"] if c.telegram_chat_id else [])
        contacts.append(Target(c.name, "emergency", email=c.email, chat_id=c.telegram_chat_id, contact=c, channels=chs))
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
    with transaction.atomic():
        # Row lock so two workers cannot send the same step twice (real on Postgres; SQLite serialises writers).
        # Re-reading under the lock also makes a stale in-memory copy safe.
        locked = CheckIn.objects.select_for_update().filter(pk=checkin.pk).first()
        if locked is None:
            return 0
        checkin.refresh_from_db()
        return _advance_locked(checkin, now)


def _advance_locked(checkin, now):
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
    if kind == CheckIn.QUIET:
        _consult_advisor(checkin, h, assessment)
    advance(checkin, now)
    return checkin


def _consult_advisor(checkin, h, assessment):
    """Optional, advisory only. May record a note, or (for 'escalate') skip the soft nudge. Can never suppress or
    delay: the deterministic chain below runs regardless, and any failure here is swallowed."""
    try:
        from .aws import advisor
        advice = advisor.advise(h, assessment)
        if advice is None:
            return
        checkin.advice = advice.note()
        if advice.action == "escalate":
            chain = build_chain(h, checkin.kind)
            if chain and chain[0].kind == "nudge":
                checkin.level = 1        # go straight to caregivers; the nudge is skipped, nothing else changes
        checkin.save()
    except Exception:  # noqa
        log.exception("advisor consult failed; continuing with the standard chain")


def acknowledge(checkin: CheckIn, resolution="all_ok", user=None, name="", now=None, contact=None):
    """all_ok / person_ok close the chain; needs_help immediately alerts every remaining step as urgent."""
    now = now or timezone.now()
    who = name or (user.get_full_name() or user.username if user else "")
    with transaction.atomic():
        CheckIn.objects.select_for_update().filter(pk=checkin.pk).first()   # serialise with advance() on Postgres
        checkin.refresh_from_db()
        return _acknowledge_locked(checkin, resolution, user, who, now, contact)


def _acknowledge_locked(checkin, resolution, user, who, now, contact):
    if checkin.state in (CheckIn.ACK,) and resolution != "needs_help":
        return checkin
    prior = set(checkin.notifications.exclude(user=None).values_list("user_id", flat=True))
    prior_contacts = set(checkin.notifications.exclude(contact=None).values_list("contact_id", flat=True))
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
    _inform(checkin, prior - ({user.pk} if user else set()), who, resolution, now, prior_contacts - ({contact.pk} if contact else set()))
    return checkin


def acknowledge_by_token(token, resolution="all_ok", now=None):
    from .models import Notification
    n = Notification.objects.select_related("checkin__household").filter(ack_token=token).first()
    if not n:
        return None
    return acknowledge(n.checkin, resolution, user=n.user, name=n.recipient, now=now, contact=n.contact)


def _inform(checkin, prior_user_ids, who, resolution, now, prior_contact_ids=()):
    """Tell caregivers (email, and Telegram if linked) and emergency contacts already messaged that someone has it."""
    from .models import EmergencyContact, Notification
    if checkin.kind == CheckIn.SENSOR:
        return
    h = checkin.household
    tg_sent = set(checkin.notifications.filter(channel="telegram", status="sent").exclude(user=None).values_list("user_id", flat=True))
    text = (f"{who or 'Someone'} says {h.name} needs help." if resolution == "needs_help"
            else f"{who or 'Someone'} confirmed {h.name} is OK. No further action needed.")

    def send(recipient, channel, dest, fn, **who_):
        n = Notification.objects.create(checkin=checkin, step=99, step_kind="info", recipient=recipient,
                                        destination=dest, channel=channel, text=text, created_at=now, **who_)
        try:
            r = fn()
            n.status, n.detail = r.status, "follow-up so nobody double-calls" if r.status == "sent" else r.detail
        except Exception as e:  # noqa
            n.status, n.detail = "failed", f"{type(e).__name__}"[:200]
        n.save()

    for m in h.memberships.filter(user_id__in=prior_user_ids).select_related("user", "user__profile"):
        u = m.user
        if u.email:
            send(str(u), "email", notify.channels.mask(u.email), lambda u=u: notify.channels.email(u.email, f"Update: {h.name}", text), user=u)
        chat = getattr(getattr(u, "profile", None), "telegram_chat_id", None)
        if chat and m.notify_telegram and u.pk in tg_sent:   # never follow up on a message quiet hours held back
            send(str(u), "telegram", "Telegram", lambda chat=chat: notify.channels.telegram(chat, text), user=u)
    for c in EmergencyContact.objects.filter(pk__in=prior_contact_ids, household=h).exclude(telegram_chat_id=None):
        send(c.name, "telegram", "Telegram", lambda c=c: notify.channels.telegram(c.telegram_chat_id, text), contact=c)
