import hashlib
import hmac
import json
from datetime import datetime, time, timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from .models import CheckIn, Event, Household
from .rules import is_quiet
from .tasks import check_quiet_mornings


def at(h, m=0):
    return timezone.make_aware(datetime.combine(timezone.localdate(), time(h, m)))


class QuietMorningTests(TestCase):
    def setUp(self):
        self.h = Household.objects.create(name="Mama", ring_location_id="loc1", window_start=time(5),
                                          check_by=time(9), caregiver_contact="+234...")

    def test_no_alert_before_deadline(self):
        self.assertFalse(is_quiet(self.h, at(8, 59)))

    def test_alert_when_quiet_and_only_once(self):
        with mock.patch("watch.tasks.timezone.now", return_value=at(9, 5)):
            self.assertEqual(check_quiet_mornings(), 1)
            self.assertEqual(check_quiet_mornings(), 0)
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_activity_suppresses_alert(self):
        Event.objects.create(household=self.h, kind="motion", at=at(6, 30))
        self.assertFalse(is_quiet(self.h, at(9, 5)))

    def test_yesterdays_activity_does_not_count(self):
        Event.objects.create(household=self.h, kind="motion", at=at(7) - timedelta(days=1))
        self.assertTrue(is_quiet(self.h, at(9, 5)))

    def test_webhook_signature_and_strips_media(self):
        body = json.dumps({"location_id": "loc1", "event_type": "motion", "video_url": "x"}).encode()
        sig = hmac.new(settings.RING_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        bad = self.client.post("/webhooks/ring/", body, content_type="application/json")
        self.assertEqual(bad.status_code, 403)
        ok = self.client.post("/webhooks/ring/", body, content_type="application/json",
                              HTTP_X_RING_SIGNATURE=sig)
        self.assertEqual(ok.status_code, 204)
        self.assertNotIn("video_url", Event.objects.get().raw)


from django.core import mail
from django.core.management import call_command
from django.test import override_settings

from .notify import send_check_in
from .rules import baseline, effective_check_by


def sign(body):
    return hmac.new(settings.RING_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def seed_history(h, first=(6, 30), days=8):
    for i in range(1, days + 1):
        Event.objects.create(household=h, kind="motion", at=at(*first) - timedelta(days=i))


class BaselineTests(TestCase):
    def setUp(self):
        self.h = Household.objects.create(name="M", ring_location_id="l", window_start=time(5),
                                          check_by=time(9), caregiver_contact="x")

    def test_thin_history_uses_fixed(self):
        seed_history(self.h, days=2)
        self.assertIsNone(baseline(self.h))
        self.assertEqual(effective_check_by(self.h), time(9))

    def test_learned_deadline_tightens(self):
        seed_history(self.h, first=(6, 0))
        self.assertEqual(effective_check_by(self.h), time(6, 30))

    def test_lower_bound_floor(self):
        seed_history(self.h, first=(5, 0))
        self.assertEqual(effective_check_by(self.h), time(6, 0))  # window_start + 60

    def test_late_riser_capped(self):
        seed_history(self.h, first=(13, 0))
        self.assertEqual(effective_check_by(self.h), time(11, 0))  # check_by + 120

    def test_adaptive_off(self):
        seed_history(self.h, first=(6, 0))
        self.h.adaptive = False
        self.assertEqual(effective_check_by(self.h), time(9))

    def test_adaptive_rule_alerts_earlier(self):
        seed_history(self.h, first=(6, 0))
        self.assertTrue(is_quiet(self.h, at(6, 45)))
        self.assertFalse(is_quiet(self.h, at(6, 20)))


class WebhookHardeningTests(TestCase):
    def setUp(self):
        self.h = Household.objects.create(name="M", ring_location_id="loc1", window_start=time(5),
                                          check_by=time(9), caregiver_contact="x")

    def post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post("/webhooks/ring/", body, content_type="application/json",
                                HTTP_X_RING_SIGNATURE=sign(body))

    def test_duplicate_delivery_is_idempotent(self):
        p = {"location_id": "loc1", "event_type": "motion", "event_id": "e1"}
        self.assertEqual(self.post(p).status_code, 204)
        self.assertEqual(self.post(p).status_code, 204)
        self.assertEqual(Event.objects.count(), 1)

    def test_duplicate_without_id_uses_body_hash(self):
        p = {"location_id": "loc1", "event_type": "motion"}
        self.post(p); self.post(p)
        self.assertEqual(Event.objects.count(), 1)

    def test_stale_replay_rejected(self):
        old = (timezone.now() - timedelta(hours=2)).isoformat()
        r = self.post({"location_id": "loc1", "event_type": "motion", "created_at": old})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Event.objects.count(), 0)

    def test_fresh_timestamp_ok(self):
        r = self.post({"location_id": "loc1", "event_type": "motion", "created_at": timezone.now().isoformat()})
        self.assertEqual(r.status_code, 204)

    def test_unknown_location_and_bad_json(self):
        self.assertEqual(self.post({"location_id": "nope"}).status_code, 400)
        body = b"not json"
        r = self.client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_RING_SIGNATURE=sign(body))
        self.assertEqual(r.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get("/webhooks/ring/").status_code, 405)


class NotifyTests(TestCase):
    def setUp(self):
        self.h = Household.objects.create(name="M", ring_location_id="l", window_start=time(5), check_by=time(9),
                                          caregiver_contact="+234", caregiver_email="c@example.com")

    def test_console_and_email(self):
        with self.assertLogs("watch.notify", "WARNING"):
            done = send_check_in(self.h, None)
        self.assertEqual(done, ["console", "email"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(self.h.access_token), mail.outbox[0].body)

    @override_settings(NOTIFY_CHANNELS=["console", "whatsapp"], WHATSAPP_ENABLED=True)
    def test_whatsapp_stub_without_keys(self):
        self.assertEqual(send_check_in(self.h, None), ["console"])

    @override_settings(NOTIFY_CHANNELS=["whatsapp"], WHATSAPP_ENABLED=False)
    def test_whatsapp_off_by_default(self):
        self.assertEqual(send_check_in(self.h, None), [])


class DashboardTests(TestCase):
    def setUp(self):
        self.h = Household.objects.create(name="Mama", ring_location_id="l", window_start=time(5),
                                          check_by=time(9), caregiver_contact="x")
        self.url = f"/h/{self.h.access_token}/"

    def test_renders_timeline(self):
        Event.objects.create(household=self.h, kind="door_open", at=timezone.now())
        r = self.client.get(self.url)
        self.assertContains(r, "door_open")

    def test_bad_token_404(self):
        self.assertEqual(self.client.get("/h/00000000-0000-0000-0000-000000000000/").status_code, 404)

    def test_confirm_ok(self):
        CheckIn.objects.create(household=self.h, date=timezone.localdate())
        self.client.post(self.url, {"action": "all_ok"})
        self.assertEqual(CheckIn.objects.get().resolution, "all_ok")

    def test_pre_confirm_suppresses_alert(self):
        self.client.post(self.url, {"action": "all_ok"})
        self.assertFalse(is_quiet(self.h, at(23, 0)))

    def test_settings_saved_and_validated(self):
        self.client.post(self.url, {"action": "settings", "window_start": "06:00", "check_by": "10:00"})
        self.h.refresh_from_db()
        self.assertEqual(self.h.check_by, time(10))
        self.assertFalse(self.h.adaptive)  # unchecked box
        self.client.post(self.url, {"action": "settings", "window_start": "11:00", "check_by": "10:00"})
        self.h.refresh_from_db()
        self.assertEqual(self.h.check_by, time(10))


class SimulatorTests(TestCase):
    def test_demo_runs(self):
        call_command("simulate_demo", stdout=mock.MagicMock())
        c = CheckIn.objects.get()
        self.assertEqual(c.resolution, "all_ok")
