"""Demo: two weeks of normal mornings, then a quiet morning, check-in, caregiver confirms.
Fully offline; creates/reuses a household named 'Demo Mama'. Uses fake clock times, no Ring needed."""
from datetime import datetime, time, timedelta
from unittest import mock

from django.core.management.base import BaseCommand
from django.utils import timezone

from watch import rules
from watch.models import CheckIn, Event, Household
from watch.tasks import check_quiet_mornings


class Command(BaseCommand):
    help = "Simulate a quiet morning end to end."

    def add_arguments(self, p):
        p.add_argument("--reset", action="store_true", help="delete the demo household first")

    def say(self, msg):
        self.stdout.write(self.style.MIGRATE_HEADING(msg))

    def handle(self, *a, **o):
        if o["reset"]:
            Household.objects.filter(ring_location_id="demo-loc").delete()
        h, _ = Household.objects.get_or_create(
            ring_location_id="demo-loc",
            defaults=dict(name="Demo Mama", window_start=time(5), check_by=time(9),
                          caregiver_contact="+2340000000", caregiver_email="daughter@example.com"))
        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        at = lambda d, hh, mm: datetime.combine(d, time(hh, mm), tz)
        self.say("1. Learning a normal morning (14 days of door/motion metadata)")
        Event.objects.filter(household=h).delete()
        CheckIn.objects.filter(household=h).delete()
        for i in range(1, 15):
            d = today - timedelta(days=i)
            Event.objects.create(household=h, kind="motion", device="kitchen", at=at(d, 6, 20 + (i * 7) % 35))
            Event.objects.create(household=h, kind="door_open", device="back door", at=at(d, 8, 0))
        b = rules.baseline(h)
        self.stdout.write(f"   usual first activity {b['median_time']:%H:%M}, rarely after {b['p90_time']:%H:%M}")
        self.stdout.write(f"   adaptive check-in time: {rules.effective_check_by(h):%H:%M} (fixed setting: {h.check_by:%H:%M})")

        deadline = datetime.combine(today, rules.effective_check_by(h), tz)
        self.say("2. Today: nothing happens...")
        with mock.patch("watch.tasks.timezone.now", return_value=deadline - timedelta(minutes=5)):
            self.stdout.write(f"   {check_quiet_mornings()} check-ins at 5 min before the deadline")
        self.say("3. Deadline passes, still quiet")
        with mock.patch("watch.tasks.timezone.now", return_value=deadline + timedelta(minutes=1)):
            n = check_quiet_mornings()
        self.stdout.write(f"   {n} gentle check-in sent (see log/email above)")
        self.stdout.write(f"   dashboard: /h/{h.access_token}/")

        self.say("4. Caregiver calls, taps All OK")
        c = CheckIn.objects.get(household=h, date=today)
        c.resolved_at, c.resolution = timezone.now(), "all_ok"
        c.save()
        self.stdout.write(f"   resolved: {c.resolution}. Done.")
