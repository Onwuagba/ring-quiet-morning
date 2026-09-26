import json
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import charts, data, escalation, privacy, rules, summary, tgbot
from .access import household_view
from .forms import (AwayForm, EmergencyContactForm, ExpectedLateForm, InviteForm, MorningForm, PersonForm,
                    PrefsForm, SettingsForm, SignupForm)
from .models import (CheckIn, EmergencyContact, ExpectedLate, Household, Invite, Membership, Notification,
                     Profile, PushSubscription)
from .notify import site

from django.conf import settings
from django.urls import reverse


# ------------------------------------------------------------------ entry, auth, invites

def home(request):
    if not request.user.is_authenticated:
        return render(request, "watch/landing.html")
    ms = list(request.user.memberships.select_related("household"))
    if not ms:
        return redirect("onboarding", step=1)
    if len(ms) == 1:
        return redirect("dashboard", hid=ms[0].household_id)
    return render(request, "watch/households.html", {"memberships": ms})


def signup(request):
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("onboarding", step=1)
    return render(request, "watch/signup.html", {"form": form})


def accept_invite(request, token):
    inv = get_object_or_404(Invite, token=token)
    if not inv.usable:
        return render(request, "watch/invite_invalid.html", status=410)
    form = None
    if not request.user.is_authenticated:
        form = SignupForm(request.POST or None, initial={"email": inv.email})
        if request.method == "POST" and form.is_valid():
            login(request, form.save())
        elif request.method == "POST":
            return render(request, "watch/invite.html", {"inv": inv, "form": form})
    if request.user.is_authenticated and request.method == "POST":
        with transaction.atomic():
            top = inv.household.memberships.order_by("-priority").values_list("priority", flat=True).first() or 0
            Membership.objects.get_or_create(user=request.user, household=inv.household,
                                             defaults={"role": inv.role, "priority": top + 1})
            Profile.objects.get_or_create(user=request.user)
            inv.accepted_at = timezone.now()
            inv.save()
        messages.success(request, f"You now help look after {inv.household.name}.")
        return redirect("prefs", hid=inv.household_id)
    return render(request, "watch/invite.html", {"inv": inv, "form": form})


# ------------------------------------------------------------------ onboarding wizard

ONBOARDING_STEPS = ["Who", "Their morning", "Your team", "Trust and privacy"]


@login_required
def onboarding(request, step):
    if step not in (1, 2, 3, 4):
        raise Http404
    hid = request.session.get("onboarding_household")
    h = None
    if hid:
        h = Household.objects.filter(pk=hid, memberships__user=request.user, memberships__role="owner").first()
    if step > 1 and not h:
        return redirect("onboarding", step=1)
    ctx = {"step": step, "steps": list(enumerate(ONBOARDING_STEPS, 1)), "h": h}
    if step == 1:
        form = PersonForm(request.POST or None, instance=h)
        if request.method == "POST" and form.is_valid():
            new = form.instance.pk is None
            if new:
                form.instance.window_start, form.instance.check_by = time(5), time(9)
            h = form.save()
            if new:
                Membership.objects.create(user=request.user, household=h, role=Membership.OWNER, priority=1)
                Profile.objects.get_or_create(user=request.user)
            request.session["onboarding_household"] = h.pk
            return redirect("onboarding", step=2)
        ctx["form"] = form
    elif step == 2:
        form = MorningForm(request.POST or None, instance=h)
        if request.method == "POST" and form.is_valid():
            form.save()
            return redirect("onboarding", step=3)
        ctx["form"] = form
    elif step == 3:
        m = Membership.objects.get(user=request.user, household=h)
        prof, _ = Profile.objects.get_or_create(user=request.user)
        prefs = PrefsForm(request.POST or None, instance=m, initial={"phone": prof.phone}, prefix="me")
        contact = EmergencyContactForm(request.POST or None, prefix="ec")
        invite = InviteForm(request.POST or None, prefix="inv")
        if request.method == "POST":
            ok = prefs.is_valid()
            if ok:
                prefs.save()
                prof.phone = prefs.cleaned_data["phone"]
                prof.save()
                if request.POST.get("ec-name") or request.POST.get("ec-phone") or request.POST.get("ec-email"):
                    if contact.is_valid():
                        contact.instance.household = h
                        contact.save()
                    else:
                        ok = False
                if request.POST.get("inv-email"):
                    if invite.is_valid():
                        _create_invite(request, h, invite)
                    else:
                        ok = False
            if ok:
                return redirect("onboarding", step=4)
        ctx.update(prefs=prefs, contact=contact, invite=invite, contacts=h.emergency_contacts.all())
    else:
        if request.method == "POST":
            if request.POST.get("consent"):
                h.consent_at = timezone.now()
                h.save()
                request.session.pop("onboarding_household", None)
                messages.success(request, "All set. Now connect Ring so events start flowing.")
                return redirect("dashboard", hid=h.pk)
            messages.error(request, "Please confirm they know and agree before finishing.")
        ctx["guarantees"] = privacy.GUARANTEES
    return render(request, "watch/onboarding.html", ctx)


def _create_invite(request, h, form):
    inv = form.save(commit=False)
    inv.household, inv.invited_by = h, request.user
    inv.expires_at = timezone.now() + timedelta(days=7)
    inv.save()
    url = site(reverse("invite", args=[inv.token]))
    from django.core.mail import send_mail
    send_mail(f"Help look after {h.name} with Quiet Morning",
              f"{request.user.get_full_name() or request.user.username} invited you to be a {inv.role} for {h.name}.\n"
              f"Accept (valid 7 days): {url}\n\nWe only ever see door and motion metadata. No video, ever.",
              settings.DEFAULT_FROM_EMAIL, [inv.email])
    return inv


# ------------------------------------------------------------------ dashboard

def _today_checkin(h, kind=CheckIn.QUIET):
    return h.checkins.filter(date=timezone.localdate(), kind=kind).first()


def _timeline(h, now, checkins):
    start = datetime.combine(timezone.localdate(), time.min, timezone.get_current_timezone())
    items = []
    for e in h.events.filter(at__gte=start, at__lte=now):
        items.append({"at": e.at, "cls": "health" if e.is_health else "activity",
                      "text": ("Device check-in (" + e.kind.replace("_", " ") + ")") if e.is_health else e.kind.replace("_", " ").capitalize()
                      + (f" · {e.device}" if e.device else "")})
    for c in checkins:
        if not c:
            continue
        items.append({"at": c.sent_at, "cls": "checkin", "text": ("Device notice opened" if c.kind == "sensor" else "Quiet-morning check-in opened")})
        for n in c.notifications.exclude(step_kind="info"):
            items.append({"at": n.created_at, "cls": "notify", "text": f"{n.recipient} by {n.get_channel_label()}: {n.status}"})
        if c.resolved_at:
            items.append({"at": c.resolved_at, "cls": "ack", "text": f"{c.acknowledged_by or 'Someone'}: {c.resolution.replace('_', ' ')}"})
    return sorted(items, key=lambda i: i["at"])


Notification.get_channel_label = lambda self: {"telegram": "Telegram", "push": "push", "email": "email"}.get(self.channel, self.channel)


@household_view("view")
def dashboard(request, h, m):
    now = timezone.now()
    if request.method == "POST":
        return _dashboard_action(request, h, m, now)
    a = rules.assess(h, now)
    quiet, sensor = _today_checkin(h), _today_checkin(h, CheckIn.SENSOR)
    drift = rules.drift(h, now)
    show = quiet if quiet and (quiet.state != CheckIn.ACK or a.state != "ok") else None
    return render(request, "watch/dashboard.html", {
        "h": h, "m": m, "a": a, "checkin": quiet, "sensor_checkin": sensor, "now": timezone.localtime(now),
        "strip": charts.day_strip(h, a, now), "ribbon": charts.ribbon(h, now), "drift": drift,
        "spark": charts.sparkline(drift["weekly"]), "timeline": _timeline(h, now, [quiet, sensor]),
        "snooze_options": (30, 60, 120, 240), "late_form": ExpectedLateForm(), "away_form": AwayForm(),
        "webhook_url": site("/webhooks/ring/"), "has_events": h.events.exists(),
        "notifications": quiet.notifications.all() if quiet else [],
        "chain": [(s.kind, s.label) for s in escalation.build_chain(h)],
    })


def _dashboard_action(request, h, m, now):
    if not m.can_act:
        return HttpResponse("Your role is read-only.", status=403)
    act = request.POST.get("action")
    back = redirect("dashboard", hid=h.pk)
    if act in ("all_ok", "needs_help"):
        c = _today_checkin(h, request.POST.get("kind") or CheckIn.QUIET)
        if c is None and act == "needs_help":     # a caregiver raising the alarm proactively
            c = CheckIn.objects.create(household=h, date=timezone.localdate(), explanation=f"Raised manually by {request.user}.")
            c.state = CheckIn.OPEN
        if c is None:
            messages.info(request, "There is no open check-in today, so nothing to confirm.")
            return back
        escalation.acknowledge(c, act, user=request.user, now=now)
        messages.success(request, "Thanks, recorded. The others have been told." if act == "all_ok"
                         else "Noted. Everyone in the chain has been alerted. Please call them or a neighbour now.")
    elif act == "snooze":
        mins = int(request.POST.get("minutes", 0) or 0)
        h.snooze_until = now + timedelta(minutes=mins) if mins in (30, 60, 120, 240) else None
        h.save()
        messages.success(request, "Snoozed." if h.snooze_until else "Snooze cancelled.")
    elif act == "expected_late":
        f = ExpectedLateForm(request.POST)
        if f.is_valid():
            ExpectedLate.objects.update_or_create(household=h, date=timezone.localdate(),
                                                  defaults={**f.cleaned_data, "created_by": request.user})
            messages.success(request, "Got it. We will wait until then.")
        else:
            messages.error(request, "Please enter a valid time.")
    elif act == "clear_expected_late":
        ExpectedLate.objects.filter(household=h, date=timezone.localdate()).delete()
    elif act == "away_on":
        f = AwayForm(request.POST)
        if f.is_valid():
            h.away_from, h.away_until = timezone.localdate(), f.cleaned_data["until"]
            h.save()
            messages.success(request, "Away mode on. Checks are paused.")
    elif act == "away_off":
        h.away_from = h.away_until = None
        h.save()
        messages.success(request, "Away mode off. Welcome home.")
    return back


@household_view("view")
def audit(request, h, m):
    checkins = h.checkins.prefetch_related("notifications").order_by("-date", "-sent_at")[:30]
    return render(request, "watch/audit.html", {"h": h, "m": m, "checkins": checkins})


@household_view("view")
def status_json(request, h, m):
    a = rules.assess(h)
    c = _today_checkin(h)
    return JsonResponse({"state": a.state, "checkin": c.state if c else None, "level": c.level if c else 0})


@household_view("view")
def summary_preview(request, h, m):
    s = summary.build(h)
    return render(request, "watch/summary.html", {"h": h, "m": m, "s": s, "text": summary.render(h, s)})


# ------------------------------------------------------------------ settings, team, prefs

@household_view("manage")
def settings_view(request, h, m):
    form = SettingsForm(request.POST or None, instance=h)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("settings", hid=h.pk)
    return render(request, "watch/settings.html", {"h": h, "m": m, "form": form, "tg_ready": bool(settings.TELEGRAM_BOT_USERNAME),
                                                   "tg_linked": tgbot.is_linked(household=h), "guarantees": privacy.GUARANTEES})


@household_view("manage")
def team(request, h, m):
    invite = InviteForm(prefix="inv")
    contact = EmergencyContactForm(prefix="ec")
    if request.method == "POST":
        act = request.POST.get("action")
        if act == "invite":
            invite = InviteForm(request.POST, prefix="inv")
            if invite.is_valid():
                _create_invite(request, h, invite)
                messages.success(request, "Invite sent. It works for 7 days.")
                return redirect("team", hid=h.pk)
        elif act == "contact":
            contact = EmergencyContactForm(request.POST, prefix="ec")
            if contact.is_valid():
                contact.instance.household = h
                contact.save()
                return redirect("team", hid=h.pk)
        elif act == "member":
            mm = get_object_or_404(Membership, pk=request.POST.get("id"), household=h)
            role = request.POST.get("role")
            if role in dict(Membership.ROLES):
                if mm.role == "owner" and role != "owner" and h.memberships.filter(role="owner").count() == 1:
                    messages.error(request, "There must be at least one owner.")
                    return redirect("team", hid=h.pk)
                mm.role = role
            mm.priority = max(1, int(request.POST.get("priority") or mm.priority))
            mm.save()
            messages.success(request, "Updated.")
            return redirect("team", hid=h.pk)
        elif act == "remove_member":
            mm = get_object_or_404(Membership, pk=request.POST.get("id"), household=h)
            if mm.role == "owner" and h.memberships.filter(role="owner").count() == 1:
                messages.error(request, "You cannot remove the only owner.")
            else:
                mm.delete()
                messages.success(request, "Removed.")
            return redirect("team", hid=h.pk)
        elif act == "remove_contact":
            EmergencyContact.objects.filter(pk=request.POST.get("id"), household=h).delete()
            return redirect("team", hid=h.pk)
        elif act == "revoke_invite":
            Invite.objects.filter(pk=request.POST.get("id"), household=h, accepted_at=None).delete()
            return redirect("team", hid=h.pk)
    return render(request, "watch/team.html", {
        "h": h, "m": m, "members": h.memberships.select_related("user"), "contacts": [(c, tgbot.is_linked(contact=c)) for c in h.emergency_contacts.all()],
        "tg_ready": bool(settings.TELEGRAM_BOT_USERNAME),
        "invites": [i for i in h.invites.filter(accepted_at=None) if i.usable], "invite": invite, "contact": contact,
        "roles": Membership.ROLES})


@household_view("view")
def prefs(request, h, m):
    prof, _ = Profile.objects.get_or_create(user=request.user)
    form = PrefsForm(request.POST or None, instance=m, initial={"phone": prof.phone, "large_type": prof.large_type})
    if request.method == "POST" and form.is_valid():
        form.save()
        prof.phone, prof.large_type = form.cleaned_data["phone"], form.cleaned_data["large_type"]
        prof.save()
        messages.success(request, "Preferences saved.")
        return redirect("prefs", hid=h.pk)
    return render(request, "watch/prefs.html", {"h": h, "m": m, "form": form, "vapid": settings.VAPID_PUBLIC_KEY,
                                                "tg_ready": bool(settings.TELEGRAM_BOT_USERNAME),
                                                "tg_linked": tgbot.is_linked(user=request.user),
                                                "push_count": request.user.push_subscriptions.count()})


@household_view("view")
@require_POST
def telegram_link(request, h, m):
    """Issue a one-time t.me deep link. Self for any member; emergency contacts and the person need manage rights."""
    who = request.POST.get("who", "me")
    if not settings.TELEGRAM_BOT_USERNAME:
        messages.error(request, "Telegram is not set up on this server yet (TELEGRAM_BOT_USERNAME).")
        return redirect("prefs", hid=h.pk)
    if who == "me":
        link, label, back = tgbot.new_link(user=request.user), "you", "prefs"
    elif not m.can_manage:
        raise Http404
    elif who == "person":
        link, label, back = tgbot.new_link(household=h), h.name, "settings"
    else:
        c = get_object_or_404(EmergencyContact, pk=request.POST.get("id"), household=h)
        link, label, back = tgbot.new_link(contact=c), c.name, "team"
    return render(request, "watch/telegram_link.html", {"h": h, "m": m, "link": tgbot.deep_link(link), "label": label,
                                                        "back": back})


@login_required
@require_POST
def toggle_large_type(request):
    p, _ = Profile.objects.get_or_create(user=request.user)
    p.large_type = not p.large_type
    p.save()
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/") else "home")


@login_required
@require_POST
def push_subscribe(request):
    try:
        d = json.loads(request.body)
        ep, keys = d["endpoint"], d["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return HttpResponseBadRequest("bad subscription")
    if not str(ep).startswith("https://"):
        return HttpResponseBadRequest("bad endpoint")
    PushSubscription.objects.update_or_create(endpoint=ep, defaults={"user": request.user, "p256dh": p256dh, "auth": auth})
    return JsonResponse({"ok": True})


# ------------------------------------------------------------------ privacy, export, delete

def privacy_page(request):
    return render(request, "watch/privacy.html", {"guarantees": privacy.GUARANTEES, "never": privacy.NEVER_STORED,
                                                  "allowed": privacy.ALLOWED_KEYS})


@household_view("manage")
def export(request, h, m):
    r = JsonResponse(data.export_household(h), json_dumps_params={"indent": 2})
    r["Content-Disposition"] = f'attachment; filename="quiet-morning-{h.pk}.json"'
    return r


@household_view("manage")
def delete(request, h, m):
    if request.method == "POST":
        if request.POST.get("confirm", "").strip() == h.name:
            data.delete_household(h)
            messages.success(request, "Everything about this household has been deleted.")
            return redirect("home")
        messages.error(request, "Type the name exactly to confirm.")
    return render(request, "watch/delete.html", {"h": h, "m": m})


# ------------------------------------------------------------------ one-tap acknowledgement (from a message)

def ack(request, token):
    n = get_object_or_404(Notification.objects.select_related("checkin__household"), ack_token=token)
    c = n.checkin
    if request.method == "POST":
        act = request.POST.get("action")
        if act in ("all_ok", "needs_help"):
            resolution = "person_ok" if (n.step_kind == "nudge" and act == "all_ok") else act
            escalation.acknowledge_by_token(token, resolution)
            c.refresh_from_db()
    return render(request, "watch/ack.html", {"n": n, "c": c, "h": c.household})


# ------------------------------------------------------------------ PWA

@require_GET
def manifest(request):
    from django.templatetags.static import static
    return JsonResponse({
        "name": "Quiet Morning", "short_name": "Quiet Morning", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#fbf7f0", "theme_color": "#2f5d62",
        "description": "A gentle morning check-in for the people who care for someone living alone.",
        "icons": [{"src": static("watch/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": static("watch/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }, content_type="application/manifest+json")


@require_GET
def service_worker(request):
    r = render(request, "watch/sw.js", content_type="application/javascript")
    r["Service-Worker-Allowed"] = "/"
    r["Cache-Control"] = "no-cache"
    return r
