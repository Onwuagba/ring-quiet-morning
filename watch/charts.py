"""Small server-side visual helpers: day strip, 14-day ribbon, drift sparkline (inline SVG, no JS)."""
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.html import format_html

from . import rules
from .models import CheckIn


def _pct(t: time, lo: int, hi: int) -> float:
    return max(0.0, min(100.0, (t.hour * 60 + t.minute - lo) / (hi - lo) * 100))


def day_strip(h, a, now):
    """Horizontal strip for today: window start, activity dots, usual time, deadline, now."""
    local = timezone.localtime(now)
    lo = max(0, (h.window_start.hour - 1) * 60)
    hi = min(24 * 60, max((a.deadline.hour + 3) * 60, lo + 8 * 60))
    marks = [{"kind": "window", "pct": _pct(h.window_start, lo, hi), "label": f"Day starts {h.window_start:%H:%M}"}]
    if a.usual:
        marks.append({"kind": "usual", "pct": _pct(a.usual, lo, hi), "label": f"Usually {a.usual:%H:%M}"})
    marks.append({"kind": "deadline", "pct": _pct(a.deadline.timetz(), lo, hi), "label": f"Check-in {a.deadline:%H:%M}"})
    start = datetime.combine(local.date(), time.min, timezone.get_current_timezone())
    for e in h.events.filter(at__gte=start, at__lte=now).order_by("at"):
        if not e.is_health:
            marks.append({"kind": "event", "pct": _pct(timezone.localtime(e.at).time(), lo, hi),
                          "label": f"{e.kind.replace('_', ' ')} {timezone.localtime(e.at):%H:%M}"})
    marks.append({"kind": "now", "pct": _pct(local.time(), lo, hi), "label": f"Now {local:%H:%M}"})
    ticks = [{"pct": (m - lo) / (hi - lo) * 100, "label": f"{m // 60:02d}:00"} for m in range((lo // 60 + 1) * 60, hi, 120)]
    return {"marks": marks, "ticks": ticks}


def ribbon(h, now, days=14):
    """One cell per day, oldest first: ok (first activity time), quiet (check-in), device, away, no data."""
    today = timezone.localtime(now).date()
    firsts = rules.first_activity_by_day(h, days + 1, now)
    cis = {(c.date, c.kind): c for c in h.checkins.filter(date__gte=today - timedelta(days=days))}
    cells = []
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        if (d, CheckIn.QUIET) in cis:
            state, label = "quiet", "Quiet morning"
        elif d in firsts:
            state, label = "ok", f"{firsts[d]:%H:%M}"
        elif (d, CheckIn.SENSOR) in cis:
            state, label = "device", "Device issue"
        else:
            state, label = "none", "No data"
        cells.append({"date": d, "state": state, "label": label, "weekday": d.strftime("%a")[0]})
    return cells


def sparkline(weekly, w=280, h=60):
    """Weekly median first-activity as an inline SVG polyline. Later = lower on the chart is NOT used: later = higher."""
    if len(weekly) < 3:
        return ""
    vals = [m for _, m in weekly]
    lo, hi = min(vals) - 10, max(vals) + 10
    pts = []
    for i, v in enumerate(vals):
        x = 6 + i * (w - 12) / (len(vals) - 1)
        y = h - 6 - (v - lo) / (hi - lo) * (h - 12)
        pts.append(f"{x:.1f},{y:.1f}")
    label = f"Weekly median first activity, from {rules._fmt(rules._t(vals[0]))} to {rules._fmt(rules._t(vals[-1]))}"
    return format_html('<svg viewBox="0 0 {} {}" role="img" aria-label="{}" class="spark"><polyline points="{}" fill="none" '
                       'stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
                       '<circle cx="{}" cy="{}" r="4" fill="currentColor"/></svg>', w, h, label, " ".join(pts),
                       pts[-1].split(",")[0], pts[-1].split(",")[1])
