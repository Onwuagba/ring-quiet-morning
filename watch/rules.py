"""The one rule: alert on the ABSENCE of activity, not on activity."""
import statistics
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import CheckIn, Household

MIN_DAYS = 5          # need this many days of history before trusting a baseline
GRACE_MIN = 30        # slack added on top of the learned "late but normal" time
MAX_EXTEND_MIN = 120  # never push the deadline more than this past the configured check_by


def _tz_now(now):
    return timezone.localtime(now)


def first_activity_by_day(household: Household, days: int = 21, before=None):
    """{date: local time of first event in the household's morning window}. Metadata only."""
    before = before or timezone.now()
    since = before - timedelta(days=days)
    firsts = {}
    today = _tz_now(before).date()
    for e in household.events.filter(at__gte=since, at__lt=before).order_by("at"):
        loc = _tz_now(e.at)
        if loc.date() == today or loc.time() < household.window_start:
            continue  # today is not history; pre-window events are not "morning"
        firsts.setdefault(loc.date(), loc.time())
    return firsts


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


def baseline(household: Household, now=None, days: int = 21):
    """Learned normal morning: median and ~90th percentile first-activity, in minutes after midnight."""
    firsts = first_activity_by_day(household, days, now)
    if len(firsts) < MIN_DAYS:
        return None
    m = sorted(_mins(t) for t in firsts.values())
    p90 = m[min(len(m) - 1, int(round(0.9 * (len(m) - 1))))]
    med = int(statistics.median(m))
    return {"days": len(m), "median": med, "p90": p90,
            "median_time": time(med // 60, med % 60), "p90_time": time(p90 // 60, p90 % 60)}


def usual_first_activity(household: Household, days: int = 14):
    b = baseline(household, days=days)
    return time(b["median"] // 60, b["median"] % 60) if b else None


def effective_check_by(household: Household, now=None) -> time:
    """Adaptive deadline: learned p90 + grace, kept between window_start+60min and check_by+120min.
    Falls back to the configured check_by when adaptive is off or history is thin."""
    if not household.adaptive:
        return household.check_by
    b = baseline(household, now)
    if not b:
        return household.check_by
    lo = _mins(household.window_start) + 60
    hi = min(_mins(household.check_by) + MAX_EXTEND_MIN, 23 * 60 + 59)
    m = max(lo, min(hi, b["p90"] + GRACE_MIN))
    return time(m // 60, m % 60)


def is_quiet(household: Household, now: datetime) -> bool:
    today = _tz_now(now).date()
    tz = timezone.get_current_timezone()
    start = datetime.combine(today, household.window_start, tz)
    deadline = datetime.combine(today, effective_check_by(household, now), tz)
    if now < deadline:
        return False
    had_activity = household.events.filter(at__gte=start, at__lte=now).exists()
    already = CheckIn.objects.filter(household=household, date=today).exists()
    return not had_activity and not already
