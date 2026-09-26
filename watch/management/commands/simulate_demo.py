"""Scripted, offline demo scenarios on a fake clock. Nothing needs Ring, Redis, a Telegram bot or the internet.

  python manage.py simulate_demo --reset                 # all scenarios, ends on the quiet-morning story
  python manage.py simulate_demo --scenario offline      # one scenario: normal | late | offline | quiet

The database is left in the state of the LAST scenario so you can open the dashboard and film it.
Log in as amina / demo-pass-12345 (owner) or tunde / demo-pass-12345 (second caregiver)."""
import logging
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from watch import escalation, rules, telegram as tg, tgbot
from watch.models import (CheckIn, DeviceStatus, EmergencyContact, Event, ExpectedLate, Household, Membership,
                          Notification, Profile)
from watch.tasks import tick

PASSWORD = "demo-pass-12345"
DEMO_CHATS = {"mama": 9001, "amina": 9002, "tunde": 9003, "aunty": 9004}   # fake chat ids, used only by the fake transport
SCENARIOS = ["normal", "late", "offline", "quiet"]


@contextmanager
def clock(dt):
    with mock.patch("django.utils.timezone.now", return_value=dt):
        yield


class Command(BaseCommand):
    help = "Play scripted Quiet Morning scenarios: normal day, late morning, sensor offline, real quiet morning."

    def add_arguments(self, p):
        p.add_argument("--scenario", choices=SCENARIOS + ["all"], default="all")
        p.add_argument("--reset", action="store_true", help="delete the demo household and users first")

    # ---- output helpers
    def say(self, msg):
        self.stdout.write("\n" + self.style.MIGRATE_HEADING(msg))

    def line(self, msg):
        self.stdout.write("   " + msg)

    # ---- setup
    def handle(self, *a, **o):
        User = get_user_model()
        if o["reset"]:
            Household.objects.filter(ring_location_id="demo-loc").delete()
            User.objects.filter(username__in=["amina", "tunde"]).delete()
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"   # keep the demo output readable
        logging.getLogger("watch").setLevel(logging.ERROR)
        self.fake_tg = not settings.TELEGRAM_BOT_TOKEN
        if self.fake_tg:
            tg.set_transport(self.fake_transport)   # no token: messages are printed, not sent
        self.tz = timezone.get_current_timezone()
        self.today = timezone.localdate()
        self.h = self.setup_household(User)
        names = SCENARIOS if o["scenario"] == "all" else [o["scenario"]]
        for name in names:
            getattr(self, "scenario_" + name)()
        tg.set_transport(None)
        self.say("Done")
        self.line(f"Open http://localhost:8000/h/{self.h.pk}/  and log in as amina / {PASSWORD}")
        self.line("The database now holds the state of the last scenario: " + names[-1])

    def fake_transport(self, method, payload):
        self.msg_id = getattr(self, "msg_id", 100) + 1
        if method == "sendMessage":
            btns = [b["text"] for row in (payload.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]
            who = {DEMO_CHATS[c]: c for c in DEMO_CHATS}.get(payload["chat_id"], "?")
            self.line(f"[Telegram -> {who}] {payload['text'][:110]}" + (f"   buttons: {' | '.join(btns)}" if btns else ""))
            return {"message_id": self.msg_id}
        if method == "editMessageText":
            self.line(f"[Telegram edit] {payload['text'][:110]}")
        return True

    def press(self, user, button_prefix):
        """Simulate the caregiver tapping a button on their latest Telegram message."""
        n = Notification.objects.filter(user__username=user, channel="telegram", step_kind="caregiver").exclude(
            telegram_message_id=None).last()
        chat = DEMO_CHATS[user]
        tgbot.handle_update({"callback_query": {"id": "1", "from": {"id": chat}, "data": f"{button_prefix}:{n.ack_token.hex}",
                                                "message": {"message_id": n.telegram_message_id, "chat": {"id": chat}}}})

    def setup_household(self, User):
        h, created = Household.objects.get_or_create(
            ring_location_id="demo-loc",
            defaults=dict(name="Mama Ngozi", window_start=time(5), check_by=time(9), person_telegram_chat_id=DEMO_CHATS["mama"],
                          nudge_enabled=True, escalation_minutes=20, consent_at=timezone.now(),
                          latitude=6.52, longitude=3.38, daylight_aware=True))
        amina, _ = User.objects.get_or_create(username="amina", defaults=dict(first_name="Amina", email="amina@example.com"))
        tunde, _ = User.objects.get_or_create(username="tunde", defaults=dict(first_name="Tunde", email="tunde@example.com"))
        for u, ph in ((amina, "+2340000002"), (tunde, "+2340000003")):
            u.set_password(PASSWORD)
            u.save()
            Profile.objects.update_or_create(user=u, defaults={"phone": ph, "telegram_chat_id": DEMO_CHATS[u.username]})
        # Amina works nights, so her phone is silent 06:00-08:45 unless things become urgent.
        Membership.objects.update_or_create(user=amina, household=h, defaults=dict(
            role="owner", priority=1, notify_telegram=True, notify_push=True, quiet_start=time(6), quiet_end=time(8, 45)))
        Membership.objects.update_or_create(user=tunde, household=h, defaults=dict(
            role="caregiver", priority=2, notify_telegram=True))
        EmergencyContact.objects.get_or_create(household=h, name="Aunty Ngozi", defaults=dict(
            relation="neighbour", phone="+2340000004", email="aunty@example.com", telegram_chat_id=DEMO_CHATS["aunty"]))
        return h

    def at(self, d, hh, mm=0):
        return datetime.combine(d, time(hh, mm), self.tz)

    def seed(self, days_missing=0):
        """Eight weeks of believable mornings: earlier on weekdays, lazier at weekends, plus device heartbeats."""
        h = self.h
        Event.objects.filter(household=h).delete()
        CheckIn.objects.filter(household=h).delete()
        DeviceStatus.objects.filter(household=h).delete()
        ExpectedLate.objects.filter(household=h).delete()
        h.away_from = h.away_until = h.snooze_until = None
        h.save()
        for i in range(1 + days_missing, 57):
            d = self.today - timedelta(days=i)
            weekend = d.weekday() >= 5
            base = (7, 25) if weekend else (6, 35)
            m = base[0] * 60 + base[1] + (i * 7) % (20 if weekend else 25)
            Event.objects.create(household=h, kind="motion", device="kitchen", at=self.at(d, m // 60, m % 60))
            Event.objects.create(household=h, kind="door_open", device="front door", at=self.at(d, 8, 5))
            Event.objects.create(household=h, kind="heartbeat", device="doorbell", at=self.at(d, 12, 0))
        last = self.today - timedelta(days=1 + days_missing)
        DeviceStatus.objects.create(household=h, device="doorbell", last_seen=self.at(last, 12), battery=82, online=True)
        b = rules.baseline(h, self.at(self.today, 5), weekday=self.today.weekday())
        return b

    def ring(self, kind, hh, mm, device="kitchen", **raw):
        Event.objects.create(household=self.h, kind=kind, device=device, at=self.at(self.today, hh, mm), raw=raw)

    def show(self, t, title):
        a = rules.assess(self.h, t)
        self.line(f"[{t.astimezone(self.tz):%H:%M}] {title}: {a.headline}")
        self.line(f"        {a.explanation}")
        if a.confidence:
            self.line(f"        confidence {a.confidence}% ({a.confidence_label}) that {a.confidence_kind}")
        return a

    def run_tick(self, t):
        with clock(t):
            r = tick()
        return r

    def audit(self):
        for n in Notification.objects.filter(checkin__household=self.h).order_by("created_at", "id"):
            self.line(f"  {timezone.localtime(n.created_at):%H:%M}  {n.recipient:<12} {n.channel:<8} {n.status:<9} {n.detail[:70]}")

    # ---- scenarios
    def scenario_normal(self):
        self.say("Scenario 1: a normal day")
        b = self.seed()
        self.line(f"Eight weeks learned. On {rules.WEEKDAYS[self.today.weekday()]}: usually {b['median_time']:%H:%M}, rarely after {b['p90_time']:%H:%M}.")
        m = b["median"] + 4
        self.ring("motion", m // 60, m % 60)
        self.ring("heartbeat", 5, 0, device="doorbell")
        t = self.at(self.today, m // 60, m % 60 + 20)
        self.show(t, "Ring says motion in the kitchen")
        self.line(f"check-ins started: {self.run_tick(t)['opened']}  (nothing to do, and nobody was bothered)")

    def scenario_late(self):
        self.say("Scenario 2: a late morning that is still normal")
        self.seed()
        dl = rules.effective_check_by(self.h, self.at(self.today, 5))
        self.line(f"Today's adaptive check-in time is {dl:%H:%M}, after their usual time plus a grace period.")
        self.ring("heartbeat", 5, 0, device="doorbell")
        t1 = self.at(self.today, 7, 0)
        self.show(t1, "Nothing yet")
        m = dl.hour * 60 + dl.minute - 8
        self.ring("motion", m // 60, m % 60)
        t2 = self.at(self.today, m // 60, m % 60 + 1)
        self.show(t2, "They got up a little late")
        self.line(f"check-ins started: {self.run_tick(t2)['opened']}")
        self.say("   ...and a day with a hospital appointment")
        Event.objects.filter(household=self.h, at__gte=self.at(self.today, 5), kind="motion").delete()
        ExpectedLate.objects.create(household=self.h, date=self.today, until_time=time(11), note="Hospital appointment at 10:00")
        t3 = self.at(self.today, 9, 30)
        self.show(t3, "Amina marked an expected-late note")
        self.line(f"check-ins started: {self.run_tick(t3)['opened']}  (correctly quiet: it was announced)")

    def scenario_offline(self):
        self.say("Scenario 3: the doorbell went offline (not a quiet morning)")
        self.seed(days_missing=3)
        DeviceStatus.objects.filter(household=self.h).update(battery=4, online=False)
        t = self.at(self.today, 9, 0)
        self.show(t, "Nothing for three days")
        r = self.run_tick(t)
        c = CheckIn.objects.get(household=self.h, kind="sensor")
        self.line(f"device notices opened: {r['opened']}; the emergency chain was NOT started")
        self.line(f"notice: {c.explanation[:140]}")
        self.audit()

    def scenario_quiet(self):
        self.say("Scenario 4: a real quiet morning, and the escalation chain")
        self.seed()
        self.ring("heartbeat", 5, 0, device="doorbell")
        DeviceStatus.objects.filter(household=self.h).update(battery=80, online=True, last_seen=self.at(self.today, 5))
        dl = rules.effective_check_by(self.h, self.at(self.today, 5))
        base = self.at(self.today, dl.hour, dl.minute)
        self.show(base - timedelta(minutes=5), "Before the check-in time")
        self.line(f"check-ins started: {self.run_tick(base - timedelta(minutes=5))['opened']}")
        t = base + timedelta(minutes=1)
        self.show(t, "Check-in time has passed")
        step = self.h.escalation_minutes
        nudge = min(10, step)
        self.say("   Step 1: a gentle hello to Mama herself")
        self.run_tick(t)
        self.say(f"   {nudge} min later, no reply: Amina (her phone is silent until 08:45, so email only)")
        self.run_tick(t + timedelta(minutes=nudge))
        self.say(f"   {step} min later, still nothing: Tunde, on Telegram with All OK / Need help buttons")
        self.run_tick(t + timedelta(minutes=nudge + step))
        self.say("   Tunde taps 'All OK' in Telegram")
        step = nudge + step
        c = CheckIn.objects.get(household=self.h, kind="quiet")
        with clock(t + timedelta(minutes=step + 4)):
            self.press("tunde", "ok")
        self.line(f"state: {CheckIn.objects.get(pk=c.pk).state}; the chain stopped before the emergency contact")
        self.say("   Audit trail")
        self.audit()
