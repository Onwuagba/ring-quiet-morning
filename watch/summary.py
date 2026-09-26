"""Weekly summary email: a calm digest of the last 7 days."""
import statistics
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from . import rules
from .models import CheckIn, Membership


def build(h, now=None):
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    tz = timezone.get_current_timezone()
    from datetime import datetime, time
    since = datetime.combine(today - timedelta(days=7), time.min, tz)
    firsts = rules.first_activity_by_day(h, 8, now + timedelta(days=1))
    week = {d: t for d, t in firsts.items() if d >= today - timedelta(days=7)}
    times = [t.hour * 60 + t.minute for t in week.values()]
    checkins = list(h.checkins.filter(date__gte=today - timedelta(days=7)))
    quiet = [c for c in checkins if c.kind == CheckIn.QUIET]
    slow = [(c.resolved_at - c.sent_at).total_seconds() / 60 for c in quiet if c.resolved_at]
    d = rules.drift(h, now)
    return {"days_seen": len(week), "avg_first": rules._fmt(rules._t(int(statistics.mean(times)))) if times else None,
            "quiet_count": len(quiet), "sensor_count": len([c for c in checkins if c.kind == CheckIn.SENSOR]),
            "avg_response_min": round(statistics.mean(slow)) if slow else None,
            "drift": d["message"] if d["flag"] else "", "since": since}


def render(h, s):
    lines = [f"Quiet Morning: your week with {h.name}", ""]
    try:
        from .aws import wording
        story = wording.weekly_narrative(h.name, s)
    except Exception:  # noqa - optional
        story = ""
    if story:
        lines += [story, ""]
    lines.append(f"Activity seen on {s['days_seen']} of the last 7 mornings."
                 + (f" First activity averaged {s['avg_first']}." if s["avg_first"] else ""))
    lines.append("No quiet mornings this week." if not s["quiet_count"] else
                 f"{s['quiet_count']} quiet morning check-in(s)."
                 + (f" Average time to confirm: {s['avg_response_min']} min." if s["avg_response_min"] is not None else ""))
    if s["sensor_count"]:
        lines.append(f"{s['sensor_count']} device notice(s): worth checking power, wifi and battery.")
    if s["drift"]:
        lines += ["", "Gentle heads-up: " + s["drift"]]
    lines += ["", "We only ever see door/motion metadata. No video, ever.", f"Dashboard: {settings.SITE_URL}/"]
    return "\n".join(lines)


def send_all(now=None):
    sent = 0
    for m in Membership.objects.filter(weekly_summary=True).exclude(role=Membership.VIEWER).select_related("user", "household"):
        if not m.user.email:
            continue
        s = build(m.household, now)
        send_mail(f"Your week with {m.household.name}", render(m.household, s), settings.DEFAULT_FROM_EMAIL, [m.user.email])
        sent += 1
    return sent
