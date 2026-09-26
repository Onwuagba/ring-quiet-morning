from datetime import time, timedelta

from django.utils import timezone

from watch import rules
from watch.models import CheckIn, DeviceStatus, Event, ExpectedLate, Household

from .base import Base, at, seed_history, today


class DeadlineTests(Base):
    def test_no_alert_before_deadline_and_alert_after(self):
        self.assertFalse(rules.is_quiet(self.h, at(8, 59)))
        self.assertTrue(rules.is_quiet(self.h, at(9, 5)))

    def test_activity_suppresses_and_yesterday_does_not_count(self):
        Event.objects.create(household=self.h, kind="motion", at=at(7) - timedelta(days=1))
        self.assertTrue(rules.is_quiet(self.h, at(9, 5)))
        Event.objects.create(household=self.h, kind="motion", at=at(6, 30))
        self.assertFalse(rules.is_quiet(self.h, at(9, 5)))

    def test_heartbeat_is_not_activity(self):
        Event.objects.create(household=self.h, kind="heartbeat", device="doorbell", at=at(6, 30))
        a = rules.assess(self.h, at(9, 5))
        self.assertEqual(a.state, "quiet")
        self.assertIn("device is online", a.explanation)

    def test_thin_history_uses_fixed_deadline(self):
        seed_history(self.h, days=2)
        self.assertIsNone(rules.baseline(self.h))
        self.assertEqual(rules.effective_check_by(self.h), time(9))

    def test_learned_deadline_tightens_and_adaptive_off_ignores(self):
        seed_history(self.h)
        self.assertLess(rules.effective_check_by(self.h, at(9)), time(9))
        self.h.adaptive = False
        self.assertEqual(rules.effective_check_by(self.h, at(9)), time(9))

    def test_deadline_never_runs_past_cap(self):
        seed_history(self.h, first=(11, 0))
        self.assertLessEqual(rules.effective_check_by(self.h, at(12)), time(11, 0))


class WeekdayBaselineTests(Base):
    def test_per_weekday_baseline_used_when_enough_samples(self):
        now = at(12)
        target = now.weekday()
        for i in range(1, 57):
            d = now - timedelta(days=i)
            m = 8 * 60 if d.weekday() == target else 6 * 60 + 30
            Event.objects.create(household=self.h, kind="motion", at=at(m // 60, m % 60, days_ago=i))
        b = rules.baseline(self.h, now, weekday=target)
        self.assertEqual(b["scope"], "weekday")
        self.assertEqual(b["median_time"], time(8, 0))
        other = rules.baseline(self.h, now, weekday=(target + 1) % 7)
        self.assertEqual(other["median_time"], time(6, 30))

    def test_falls_back_to_all_days(self):
        seed_history(self.h, days=6)
        b = rules.baseline(self.h, at(12), weekday=(at(12).weekday() + 3) % 7)
        self.assertIsNotNone(b)


class AssessmentStateTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h)

    def test_ok_explained(self):
        Event.objects.create(household=self.h, kind="motion", at=at(6, 40))
        a = rules.assess(self.h, at(7))
        self.assertEqual(a.state, "ok")
        self.assertIn("06:40", a.explanation)

    def test_waiting_then_quiet_with_explanation_and_confidence(self):
        a = rules.assess(self.h, at(6, 45))
        self.assertEqual(a.state, "waiting")
        self.assertIn("Usually first activity around 06:30", a.explanation)
        a = rules.assess(self.h, at(9, 0))
        self.assertEqual(a.state, "quiet")
        self.assertIn("today nothing by 09:00", a.explanation)
        self.assertEqual(a.confidence_label, "Medium")   # only 8 days of history
        seed_history(self.h, days=56)
        Event.objects.filter(kind="motion", at__lt=at(0, 0, days_ago=8)).exclude(at__gte=at(0, 0, days_ago=9)).count()
        self.assertEqual(rules.assess(self.h, at(9, 0)).confidence_label, "High")

    def test_low_battery_lowers_confidence(self):
        good = rules.assess(self.h, at(9, 0)).confidence
        DeviceStatus.objects.create(household=self.h, device="doorbell", last_seen=at(8), battery=5)
        a = rules.assess(self.h, at(9, 0))
        self.assertEqual(a.state, "quiet")
        self.assertLess(a.confidence, good)

    def test_away_mode_pauses(self):
        self.h.away_from, self.h.away_until = today() - timedelta(days=1), today() + timedelta(days=2)
        self.assertEqual(rules.assess(self.h, at(10)).state, "away")
        self.assertFalse(rules.is_quiet(self.h, at(10)))
        self.h.away_until = today() - timedelta(days=0) - timedelta(days=0)
        self.h.away_until = None
        self.assertTrue(self.h.is_away())
        self.h.away_from = today() + timedelta(days=3)
        self.assertFalse(self.h.is_away())

    def test_snooze(self):
        self.h.snooze_until = at(10)
        self.assertEqual(rules.assess(self.h, at(9, 30)).state, "snoozed")
        self.assertEqual(rules.assess(self.h, at(10, 30)).state, "quiet")

    def test_expected_late_moves_deadline_with_note(self):
        ExpectedLate.objects.create(household=self.h, date=today(), until_time=time(11), note="Hospital")
        a = rules.assess(self.h, at(9, 30))
        self.assertEqual(a.state, "waiting")
        self.assertIn("Hospital", a.explanation)
        self.assertEqual(rules.assess(self.h, at(11, 5)).state, "quiet")

    def test_device_silent_for_days_is_sensor_issue_not_quiet(self):
        Event.objects.all().delete()
        seed_history(self.h, days=8)
        Event.objects.filter(at__gte=at(0, 0, days_ago=4)).delete()
        a = rules.assess(self.h, at(9, 0))
        self.assertEqual(a.state, "sensor_issue")
        self.assertTrue(a.needs_sensor and not a.needs_check)
        self.assertFalse(rules.is_quiet(self.h, at(9, 0)))
        self.assertIn("days", a.explanation)

    def test_offline_device_flag(self):
        DeviceStatus.objects.create(household=self.h, device="doorbell", last_seen=at(3), online=False)
        self.assertEqual(rules.assess(self.h, at(9, 0)).state, "sensor_issue")

    def test_low_battery_only_is_not_sensor_issue(self):
        DeviceStatus.objects.create(household=self.h, device="doorbell", last_seen=at(3), battery=8)
        self.assertEqual(rules.sensor_health(self.h, at(9))["status"], "battery_low")


class DaylightTests(Base):
    def test_sunrise_lagos_is_plausible_and_seasonal(self):
        from datetime import date
        jun = rules.sunrise_minutes(6.5, 3.4, date(2026, 6, 21))
        dec = rules.sunrise_minutes(6.5, 3.4, date(2026, 12, 21))
        self.assertTrue(6 * 60 < jun < 7 * 60 and 6 * 60 < dec < 7.5 * 60)
        self.assertGreater(dec, jun)
        # high latitude swings more
        self.assertGreater(rules.sunrise_minutes(60, 10, date(2026, 12, 21)) - rules.sunrise_minutes(60, 10, date(2026, 6, 21)), 200)
        self.assertIsNone(rules.sunrise_minutes(80, 0, date(2026, 6, 21)))

    def test_shift_applied_only_when_enabled(self):
        h = self.h
        h.latitude, h.longitude = 51.5, -0.1
        d_winter = [today().replace(month=1, day=10)]
        self.assertEqual(rules.daylight_shift(h, today().replace(month=6, day=21), d_winter), 0)  # disabled
        h.daylight_aware = True
        self.assertLess(rules.daylight_shift(h, today().replace(month=6, day=21), d_winter), 0)  # earlier sunrise
        self.assertGreaterEqual(rules.daylight_shift(h, today().replace(month=6, day=21), d_winter), -30)


class DriftTests(Base):
    def test_flags_gradual_later_shift(self):
        for i in range(1, 57):
            m = 6 * 60 + 30 + (60 if i <= 12 else 0)
            Event.objects.create(household=self.h, kind="motion", at=at(m // 60, m % 60, days_ago=i))
        d = rules.drift(self.h, at(12))
        self.assertTrue(d["flag"])
        self.assertGreaterEqual(d["delta"], 40)
        self.assertIn("later", d["message"])
        self.assertTrue(d["weekly"])

    def test_stable_routine_no_flag(self):
        seed_history(self.h, days=40)
        self.assertFalse(rules.drift(self.h, at(12))["flag"])

    def test_not_enough_data(self):
        seed_history(self.h, days=3)
        self.assertFalse(rules.drift(self.h, at(12))["flag"])
