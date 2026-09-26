"""The signal engine. One idea: alert on the ABSENCE of expected activity, and say why.

Everything here is a pure function of (household, now) so it can be tested and replayed with a fake clock.
Only event metadata is read. Every decision comes back as an `Assessment` with a plain-English explanation.
"""
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.utils import timezone

from .models import HEALTH_KINDS, CheckIn, ExpectedLate, Household

MIN_DAYS = 5            # days of history before trusting an overall baseline
MIN_WEEKDAY = 3         # same-weekday samples needed to use a per-weekday baseline
GRACE_MIN = 30          # slack added on top of the learned "late but normal" time
MAX_EXTEND_MIN = 120    # never push the deadline more than this past the configured check_by
LOOKBACK_DAYS = 56      # eight weeks of history
MORNING_END = time(14, 0)  # first activity after this is not "morning"
LOW_BATTERY = 15
DRIFT_MIN_SHIFT = 40    # minutes later (recent vs earlier) before we gently flag drift
WEEKDAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]


def _local(dt):
    return timezone.localtime(dt)


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


def _t(m: int) -> time:
    m = max(0, min(23 * 60 + 59, int(m)))
    return time(m // 60, m % 60)


def _fmt(t) -> str:
    return f"{t:%H:%M}"


# ----------------------------------------------------------------- daylight

def sunrise_minutes(lat: float, lon: float, d: date) -> Optional[float]:
    """Approximate local sunrise, minutes after midnight (NOAA low-precision formula). None in polar day/night."""
    n = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    latr = math.radians(lat)
    x = math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decl)) - math.tan(latr) * math.tan(decl)
    if not -1 <= x <= 1:
        return None
    ha = math.degrees(math.acos(x))
    utc = 720 - 4 * (lon + ha) - eqtime
    noon = datetime.combine(d, time(12), timezone.get_current_timezone())
    return utc + noon.utcoffset().total_seconds() / 60


def daylight_shift(h: Household, today: date, sample_dates) -> int:
    """Minutes to move today's expectations, because sunrise is earlier/later than during the sample days.
    Only half the sunrise change is applied (people follow the sun loosely) and capped at 30 minutes."""
    if not (h.daylight_aware and h.latitude is not None and h.longitude is not None) or not sample_dates:
        return 0
    now_sr = sunrise_minutes(h.latitude, h.longitude, today)
    past = [sunrise_minutes(h.latitude, h.longitude, d) for d in sample_dates]
    past = [p for p in past if p is not None]
    if now_sr is None or not past:
        return 0
    return int(max(-30, min(30, round(0.5 * (now_sr - statistics.mean(past))))))


# ----------------------------------------------------------------- history

def first_activity_by_day(h: Household, days: int = LOOKBACK_DAYS, before=None):
    """{date: local time of first *activity* event in the morning}. Health events (heartbeats) never count."""
    before = before or timezone.now()
    days = min(days, max(h.retention_days, 14))
    today = _local(before).date()
    firsts = {}
    for e in h.events.filter(at__gte=before - timedelta(days=days), at__lt=before).order_by("at"):
        if e.is_health:
            continue
        loc = _local(e.at)
        if loc.date() == today or loc.time() < h.window_start or loc.time() > MORNING_END:
            continue  # today is not history; pre-window / afternoon events are not "the morning"
        firsts.setdefault(loc.date(), loc.time())
    return firsts


def baseline(h: Household, now=None, days: int = LOOKBACK_DAYS, weekday: Optional[int] = None):
    """Learned normal morning. Uses same-weekday history when there is enough, else all days.
    Times are minutes after midnight."""
    now = now or timezone.now()
    firsts = first_activity_by_day(h, days, now)
    items = sorted(firsts.items())
    scope = "all"
    if weekday is not None:
        same = [(d, t) for d, t in items if d.weekday() == weekday]
        if len(same) >= MIN_WEEKDAY:
            items, scope = same, "weekday"
    if len(items) < (MIN_WEEKDAY if scope == "weekday" else MIN_DAYS):
        return None
    m = sorted(_mins(t) for _, t in items)
    shift = daylight_shift(h, _local(now).date(), [d for d, _ in items])
    p90 = m[min(len(m) - 1, int(round(0.9 * (len(m) - 1))))] + shift
    med = int(statistics.median(m)) + shift
    return {"days": len(m), "total_days": len(firsts), "scope": scope, "median": med, "p90": p90, "spread": p90 - med, "daylight_shift": shift,
            "median_time": _t(med), "p90_time": _t(p90),
            "weekday_name": WEEKDAYS[weekday] if weekday is not None and scope == "weekday" else ""}


def usual_first_activity(h: Household, days: int = 14):
    b = baseline(h, days=days)
    return b["median_time"] if b else None


def effective_check_by(h: Household, now=None) -> time:
    """Deadline for today: learned p90 + grace (per weekday), bounded, or an 'expected late' override."""
    now = now or timezone.now()
    local = _local(now)
    base = h.check_by
    if h.adaptive:
        b = baseline(h, now, weekday=local.weekday())
        if b:
            lo = _mins(h.window_start) + 60
            hi = min(_mins(h.check_by) + MAX_EXTEND_MIN, 23 * 60 + 59)
            base = _t(max(lo, min(hi, b["p90"] + GRACE_MIN)))
    late = ExpectedLate.objects.filter(household=h, date=local.date()).first()
    if late and late.until_time > base:
        return late.until_time
    return base


# ----------------------------------------------------------------- sensor health

def sensor_health(h: Household, now=None):
    """Is the *device* healthy? Separates 'device offline / low battery / silent for days' from 'quiet morning'."""
    now = now or timezone.now()
    last = h.events.filter(at__lte=now).order_by("-at").first()
    issues, status = [], "healthy"
    last_seen = last.at if last else None
    if last is None:
        status = "no_data"
    else:
        gap = now - last.at
        if gap > timedelta(hours=h.sensor_silent_hours):
            status = "offline"
            issues.append(f"No events of any kind from any device for {_ago(gap)}")
    devices = list(h.devices.all())
    off = [d for d in devices if not d.online]
    if devices and len(off) == len(devices):
        status = "offline"
        issues.append("All devices report offline")
    elif off:
        status = status if status != "healthy" else "degraded"
        issues.append("Offline: " + ", ".join(d.device for d in off))
    low = [d for d in devices if d.battery is not None and d.battery <= LOW_BATTERY]
    if low:
        if status == "healthy":
            status = "battery_low"
        issues.append("Low battery: " + ", ".join(f"{d.device} ({d.battery}%)" for d in low))
    heartbeat = h.events.filter(at__gte=now - timedelta(hours=12), at__lte=now).exists()
    return {"status": status, "issues": issues, "last_seen": last_seen, "heartbeat_recent": heartbeat,
            "devices": devices, "healthy": status in ("healthy", "battery_low", "degraded", "no_data")}


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24} days" if hours >= 48 else f"{hours} hours"


# ----------------------------------------------------------------- assessment

@dataclass
class Assessment:
    state: str                      # ok waiting quiet sensor_issue away snoozed
    headline: str
    explanation: str
    confidence: int = 0
    confidence_kind: str = ""       # what the confidence is about
    deadline: Optional[datetime] = None
    usual: Optional[time] = None
    first_today: Optional[datetime] = None
    baseline: Optional[dict] = None
    health: dict = field(default_factory=dict)
    needs_check: bool = False       # start a quiet-morning escalation
    needs_sensor: bool = False      # tell caregivers about a device problem instead
    expected_late_note: str = ""

    @property
    def confidence_label(self):
        return "High" if self.confidence >= 75 else "Medium" if self.confidence >= 50 else "Low"


def _confidence_quiet(b, health, late_min) -> int:
    score = 45
    if b:
        score += min(25, round(b["total_days"] / 28 * 25))
        score += 5 if b["scope"] == "weekday" else 0
        score += 10 if b["spread"] <= 45 else 0
    score += min(10, max(0, late_min) // 6)
    if health["heartbeat_recent"]:
        score += 10            # device demonstrably alive, so silence is more meaningful
    if health["status"] in ("battery_low", "degraded"):
        score -= 20
    if health["last_seen"] and (timezone.now() - health["last_seen"]) > timedelta(hours=12):
        score -= 10
    return max(5, min(98, score))


def assess(h: Household, now=None) -> Assessment:
    now = now or timezone.now()
    local = _local(now)
    today = local.date()
    tz = timezone.get_current_timezone()
    health = sensor_health(h, now)
    b = baseline(h, now, weekday=local.weekday())
    usual = b["median_time"] if b else None
    day = WEEKDAYS[local.weekday()] if b and b["scope"] == "weekday" else "mornings"
    deadline_t = effective_check_by(h, now)
    deadline = datetime.combine(today, deadline_t, tz)
    start = datetime.combine(today, h.window_start, tz)
    common = dict(deadline=deadline, usual=usual, baseline=b, health=health)
    late = ExpectedLate.objects.filter(household=h, date=today).first()
    late_note = (f"Expected late today until {_fmt(late.until_time)}" + (f": {late.note}" if late and late.note else "")
                 if late else "")
    common["expected_late_note"] = late_note

    if h.is_away(today):
        return Assessment("away", "Away mode is on", "Checks are paused while they are away. "
                          + (f"Back on {h.away_until:%a %d %b}." if h.away_until else "Turn it off when they are home."), **common)
    if h.is_snoozed(now):
        return Assessment("snoozed", "Snoozed", f"Alerts are snoozed until {_local(h.snooze_until):%H:%M}.", **common)

    first = h.events.filter(at__gte=start, at__lte=now).exclude(kind__in=HEALTH_KINDS).order_by("at").first()
    if first:
        f = _local(first.at)
        expl = f"{first.kind.replace('_', ' ').capitalize()} at {_fmt(f)}"
        if usual:
            diff = _mins(f.time()) - _mins(usual)
            expl += (f", about {abs(diff)} min {'later' if diff > 0 else 'earlier'} than usual ({_fmt(usual)})"
                     if abs(diff) >= 10 else f", right on routine (usually {_fmt(usual)})")
        return Assessment("ok", "Normal morning", expl + ".", first_today=first.at, **common)

    usual_txt = (f"Usually first activity around {_fmt(usual)} on {day}" if usual
                 else "Still learning their routine") + f"; today nothing by {_fmt(local.time())}."
    if now < deadline:
        wait = f" We will check in at {_fmt(deadline_t)} if it stays quiet."
        return Assessment("waiting", "Nothing yet, still early", usual_txt + wait + (" " + late_note if late_note else ""), **common)

    if not health["healthy"]:
        return Assessment("sensor_issue", "Device problem, not necessarily a quiet morning",
                          "; ".join(health["issues"]) + ". Their routine cannot be judged until the device is back, "
                          "so please check the device (power, wifi, battery) and also give them a call.",
                          confidence=min(95, 70 + 10 * len(health["issues"])), confidence_kind="device problem",
                          needs_sensor=True, **common)

    late_min = int((now - deadline).total_seconds() // 60)
    conf = _confidence_quiet(b, health, late_min)
    expl = usual_txt + f" Check-in time was {_fmt(deadline_t)}."
    if not b:
        expl += " (Not enough history yet, so the fixed time was used.)"
    if health["heartbeat_recent"]:
        expl += " The device is online, so this is not a connection issue."
    return Assessment("quiet", "Quiet morning", expl, confidence=conf, confidence_kind="something is wrong",
                      needs_check=True, **common)


def is_quiet(h: Household, now: datetime) -> bool:
    """True if a quiet-morning check-in should be started now (and none exists yet today)."""
    a = assess(h, now)
    return a.needs_check and not CheckIn.objects.filter(household=h, date=_local(now).date(), kind=CheckIn.QUIET).exists()


# ----------------------------------------------------------------- drift

def drift(h: Household, now=None):
    """Gentle early warning: has the first-activity time crept later over weeks? Never an alert."""
    now = now or timezone.now()
    firsts = first_activity_by_day(h, LOOKBACK_DAYS, now)
    today = _local(now).date()
    recent = [_mins(t) for d, t in firsts.items() if (today - d).days <= 14]
    earlier = [_mins(t) for d, t in firsts.items() if 14 < (today - d).days <= LOOKBACK_DAYS]
    weeks = {}
    for d, t in firsts.items():
        weeks.setdefault((today - d).days // 7, []).append(_mins(t))
    weekly = [(w, int(statistics.median(v))) for w, v in sorted(weeks.items(), reverse=True)]  # oldest first
    out = {"weekly": weekly, "flag": False, "delta": 0, "message": ""}
    if len(recent) >= 5 and len(earlier) >= 10:
        r, e = int(statistics.median(recent)), int(statistics.median(earlier))
        out["delta"] = r - e
        if r - e >= DRIFT_MIN_SHIFT:
            out.update(flag=True, message=(
                f"First activity has crept about {r - e} minutes later over the last two weeks "
                f"(was around {_fmt(_t(e))}, now around {_fmt(_t(r))}). Not an alert, but it may be worth a friendly call."))
    return out
