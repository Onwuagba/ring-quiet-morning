import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from . import escalation, notify, rules
from .models import CheckIn, Event, Household

log = logging.getLogger("watch.tasks")


@shared_task
def check_quiet_mornings():
    """Assess every household; open a check-in for a quiet morning or a device notice. Returns count opened."""
    now = timezone.now()
    opened = 0
    for h in Household.objects.all():
        try:
            a = rules.assess(h, now)
            today = timezone.localtime(now).date()
            if a.needs_check and not CheckIn.objects.filter(household=h, date=today, kind=CheckIn.QUIET).exists():
                if escalation.start(h, a, now, CheckIn.QUIET):
                    opened += 1
            elif a.needs_sensor and not CheckIn.objects.filter(household=h, date=today, kind=CheckIn.SENSOR).exists():
                if escalation.start(h, a, now, CheckIn.SENSOR):
                    opened += 1
        except Exception:
            log.exception("assessment failed", extra={"household": h.pk})
    return opened


@shared_task
def run_escalations():
    """Advance open check-ins whose next step is due, and retry failed sends (backoff)."""
    now = timezone.now()
    steps = 0
    for c in CheckIn.objects.filter(state=CheckIn.OPEN).select_related("household"):
        steps += escalation.advance(c, now)
    return steps + notify.retry_due(now)


@shared_task
def tick():
    return {"opened": check_quiet_mornings(), "advanced": run_escalations()}


@shared_task
def purge_old_data():
    """Enforce each household's retention window."""
    total = 0
    for h in Household.objects.all():
        n, _ = Event.objects.filter(household=h, at__lt=timezone.now() - timedelta(days=h.retention_days)).delete()
        total += n
    log.info("retention purge", extra={"deleted": total})
    return total


@shared_task
def send_weekly_summaries():
    from .summary import send_all
    return send_all()
