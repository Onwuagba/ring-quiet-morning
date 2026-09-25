import hashlib
import hmac
import json

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from datetime import datetime, time

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from . import rules
from .models import CheckIn, Event, Household


class SettingsForm(forms.ModelForm):
    window_start = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    check_by = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))

    class Meta:
        model = Household
        fields = ["window_start", "check_by", "adaptive"]

    def clean(self):
        d = super().clean()
        if d.get("window_start") and d.get("check_by") and d["check_by"] <= d["window_start"]:
            raise forms.ValidationError("Check-in time must be after the window start.")
        return d


def _valid_signature(request) -> bool:
    # TODO-verify: PLACEHOLDER scheme. The real Ring webhook signature header/algorithm is unknown to us;
    # confirm in Ring developer docs and replace. Do not treat this as Ring's actual scheme.
    sig = request.headers.get("X-Ring-Signature", "")
    expected = hmac.new(settings.RING_WEBHOOK_SECRET.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@csrf_exempt
@require_POST
def ring_webhook(request):
    if not _valid_signature(request):
        return HttpResponseForbidden()
    try:
        body = json.loads(request.body)
        household = Household.objects.get(ring_location_id=body["location_id"])
    except (ValueError, KeyError, Household.DoesNotExist):
        return HttpResponseBadRequest()
    when = parse_datetime(str(body.get("created_at", ""))) if body.get("created_at") else None
    now = timezone.now()
    if when is not None and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.utc)
    window = settings.RING_REPLAY_WINDOW_SECONDS
    if window and when is not None and not (-300 <= (now - when).total_seconds() <= window):
        return HttpResponseBadRequest("stale or future timestamp")  # replay protection
    # Idempotency: prefer Ring's event id (TODO-verify field name), else hash of the exact body.
    ext = str(body.get("event_id") or body.get("id") or hashlib.sha256(request.body).hexdigest())[:100]
    try:
      with transaction.atomic():
        Event.objects.create(
            household=household, kind=body.get("event_type", "unknown"), device=body.get("device_id", ""),
            at=when or now, external_id=ext,
            raw={k: v for k, v in body.items() if k not in ("video_url", "snapshot_url")},  # metadata only
        )
    except IntegrityError:
        pass  # duplicate delivery: acknowledge without double-recording
    return HttpResponse(status=204)


def _today_bounds():
    today = timezone.localdate()
    tz = timezone.get_current_timezone()
    return today, datetime.combine(today, time.min, tz)


def dashboard(request, token):
    h = get_object_or_404(Household, access_token=token)
    today, start = _today_bounds()
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("all_ok", "needs_help"):
            c, _ = CheckIn.objects.get_or_create(household=h, date=today)
            c.resolved_at, c.resolution = timezone.now(), action
            c.save()
            messages.success(request, "Thanks, recorded." if action == "all_ok" else "Noted. Please call them or a neighbour now.")
        elif action == "settings":
            form = SettingsForm(request.POST, instance=h)
            if form.is_valid():
                form.save()
                messages.success(request, "Settings saved.")
            else:
                messages.error(request, "Invalid settings: " + "; ".join(sum(form.errors.values(), [])))
        return redirect("dashboard", token=token)
    events = h.events.filter(at__gte=start).order_by("at")
    return render(request, "watch/dashboard.html", {
        "h": h, "events": events, "checkin": h.checkins.filter(date=today).first(),
        "baseline": rules.baseline(h), "effective": rules.effective_check_by(h), "form": SettingsForm(instance=h),
        "now": timezone.localtime(),
    })
