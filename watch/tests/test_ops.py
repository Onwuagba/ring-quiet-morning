import json
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from watch import privacy, summary, tasks
from watch.models import CheckIn, DeviceStatus, Event, Household
from watch.simulator import DEMO_ACCOUNT

from .base import Base, at, seed_history, sign, today


def post(client, payload, **kw):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_SIGNATURE=kw.get("sig", sign(body)))


class WebhookTests(Base):
    def test_bad_signature_rejected(self):
        r = self.client.post("/webhooks/ring/", b"{}", content_type="application/json", HTTP_X_SIGNATURE="nope")
        self.assertEqual(r.status_code, 403)

    def test_stores_only_allowlisted_metadata(self):
        r = post(self.client, {"location_id": "loc1", "event_type": "motion", "device_id": "kitchen", "video_url": "https://x/v.mp4",
                               "snapshot_url": "https://x/s.jpg", "thumbnail": "data:image/png;base64,AAA", "face": "Bob",
                               "nested": {"stream": "rtsp://x"}, "note": "hello", "event_id": "e1"})
        self.assertEqual(r.status_code, 204)
        raw = Event.objects.get().raw
        self.assertEqual(set(raw), {"event_type", "device_id", "event_id"})

    def test_duplicate_delivery_idempotent(self):
        p = {"location_id": "loc1", "event_type": "motion", "event_id": "same"}
        self.assertEqual(post(self.client, p).status_code, 204)
        self.assertEqual(post(self.client, p).status_code, 204)
        self.assertEqual(Event.objects.count(), 1)

    def test_replay_and_future_rejected(self):
        old = (timezone.now() - timedelta(hours=2)).isoformat()
        self.assertEqual(post(self.client, {"location_id": "loc1", "event_type": "motion", "created_at": old}).status_code, 400)
        fut = (timezone.now() + timedelta(hours=2)).isoformat()
        self.assertEqual(post(self.client, {"location_id": "loc1", "event_type": "motion", "created_at": fut}).status_code, 400)

    def test_malformed_inputs_are_400_not_500(self):
        for body in (b"not json", b"[]", b'"str"', json.dumps({"location_id": 5}).encode(),
                     json.dumps({"location_id": "nowhere"}).encode(),
                     json.dumps({"location_id": "loc1", "created_at": "garbage"}).encode(),
                     json.dumps({"location_id": "loc1", "created_at": "2026-99-99T00:00:00"}).encode()):
            self.assertEqual(post(self.client, body).status_code, 400, body)
        self.assertEqual(Event.objects.count(), 0)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get("/webhooks/ring/").status_code, 405)

    def test_health_event_updates_device_status(self):
        post(self.client, {"location_id": "loc1", "event_type": "battery", "device_id": "doorbell", "battery": 9, "event_id": "b1"})
        d = DeviceStatus.objects.get()
        self.assertEqual((d.battery, d.online), (9, True))
        post(self.client, {"location_id": "loc1", "event_type": "offline", "device_id": "doorbell", "event_id": "b2"})
        self.assertFalse(DeviceStatus.objects.get().online)

    def test_battery_out_of_range_ignored(self):
        post(self.client, {"location_id": "loc1", "event_type": "battery", "device_id": "d", "battery": 900, "event_id": "b3"})
        self.assertIsNone(DeviceStatus.objects.get().battery)

    def test_structured_logging_on_reject(self):
        with self.assertLogs("watch.webhook", "WARNING") as cm:
            post(self.client, b"not json")
        self.assertEqual(cm.records[0].reason, "invalid json")

    def test_json_formatter(self):
        import logging
        from watch.logging_utils import JsonFormatter
        rec = logging.LogRecord("watch.x", logging.INFO, "f", 1, "hello %s", ("w",), None)
        rec.household = 7
        out = json.loads(JsonFormatter().format(rec))
        self.assertEqual((out["msg"], out["household"], out["level"]), ("hello w", 7, "INFO"))


class PrivacyModelTests(TestCase):
    def test_sanitize(self):
        self.assertEqual(privacy.sanitize_payload({"event_type": "x", "video_url": "u", "battery": 5, "device_id": "http://evil"}),
                         {"event_type": "x", "battery": 5})
        self.assertEqual(privacy.sanitize_payload("nope"), {})
        self.assertEqual(privacy.sanitize_payload({"status": "a" * 500}), {})

    def test_model_layer_sanitizes_even_without_webhook(self):
        h = Household.objects.create(name="x", ring_location_id="z", window_start=at(5).time(), check_by=at(9).time())
        e = Event.objects.create(household=h, kind="motion", at=at(6), raw={"snapshot_url": "https://a/b.jpg", "event_type": "motion"})
        e.refresh_from_db()
        self.assertEqual(e.raw, {"event_type": "motion"})


class HealthTests(TestCase):
    def test_healthz(self):
        r = self.client.get("/healthz/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_healthz_deep_reports_broker_down(self):
        with mock.patch("redis.Redis.from_url", side_effect=OSError("down")):
            r = self.client.get("/healthz/?deep=1")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["broker"], "unreachable")

    def test_healthz_db_failure(self):
        with mock.patch("watch.webhook.Event.objects") as m:
            m.order_by.side_effect = Exception("db gone")
            self.assertEqual(self.client.get("/healthz/").status_code, 503)


class RetentionAndSummaryTests(Base):
    def test_purge_respects_household_retention(self):
        self.h.retention_days = 30
        self.h.save()
        Event.objects.create(household=self.h, kind="motion", at=at(6, days_ago=40))
        Event.objects.create(household=self.h, kind="motion", at=at(6, days_ago=5))
        self.assertEqual(tasks.purge_old_data(), 1)
        self.assertEqual(Event.objects.count(), 1)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_weekly_summary_email(self):
        seed_history(self.h, days=10)
        self.mt.weekly_summary = False
        self.mt.save()
        self.assertEqual(summary.send_all(), 1)
        self.assertEqual(mail.outbox[0].to, ["amina@example.com"])
        self.assertIn("Activity seen on", mail.outbox[0].body)
        self.assertIn("No video, ever", mail.outbox[0].body)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_summary_mentions_quiet_and_drift(self):
        for i in range(1, 57):
            m = 6 * 60 + 30 + (60 if i <= 12 else 0)
            Event.objects.create(household=self.h, kind="motion", at=at(m // 60, m % 60, days_ago=i))
        CheckIn.objects.create(household=self.h, date=today() - timedelta(days=2), kind="quiet")
        s = summary.build(self.h)
        text = summary.render(self.h, s)
        self.assertIn("1 quiet morning check-in", text)
        self.assertIn("Gentle heads-up", text)


class DemoTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_all_scenarios_run_and_end_on_quiet(self):
        from io import StringIO
        out = StringIO()
        call_command("simulate_demo", "--reset", stdout=out)
        text = out.getvalue()
        for s in ("normal day", "late morning", "doorbell went offline", "real quiet morning"):
            self.assertIn(s, text)
        h = Household.objects.get(ring_location_id=DEMO_ACCOUNT)
        self.assertEqual(h.checkins.get(kind="quiet").state, "acknowledged")
        self.assertEqual(h.checkins.filter(kind="sensor").count(), 0)   # reset by the last seed

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_offline_scenario_alone_sends_device_notice_not_chain(self):
        from io import StringIO
        call_command("simulate_demo", "--reset", "--scenario", "offline", stdout=StringIO())
        h = Household.objects.get(ring_location_id=DEMO_ACCOUNT)
        self.assertEqual(h.checkins.get().kind, "sensor")
        self.assertFalse(h.checkins.filter(kind="quiet").exists())


class RingEnvelopeTests(Base):
    """Ring's documented webhook format: X-Signature: sha256=<hex>, meta/data envelope, matched on meta.account_id."""

    def setUp(self):
        super().setUp()
        Household.objects.filter(pk=self.h.pk).update(ring_location_id="ava1.ring.account.TEST")

    def send(self, etype, device="kitchen-motion", **kw):
        from watch import ringapi
        body = json.dumps(ringapi.build_event("ava1.ring.account.TEST", etype, device, **kw)).encode()
        return body, self.client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_SIGNATURE=sign(body))

    def test_motion_event_is_stored_with_ring_timestamp(self):
        when = timezone.now() - timedelta(minutes=30)
        _, r = self.send("motion_detected", at=when, sub_type="human")
        self.assertEqual(r.status_code, 204)
        e = Event.objects.get()
        self.assertEqual((e.kind, e.device), ("motion", "kitchen-motion"))
        self.assertAlmostEqual(e.at.timestamp(), when.timestamp(), delta=1)
        self.assertEqual(e.raw["sub_type"], "human")
        self.assertTrue(e.external_id.startswith("kitchen-motion_motion_detected_"))

    def test_doorbell_press_is_not_resident_activity(self):
        self.send("button_press", device="doorbell")
        self.assertEqual(Event.objects.get().kind, "doorbell_press")
        self.assertTrue(Event.objects.get().is_health)

    def test_online_offline_update_device_status(self):
        self.send("device_online", device="doorbell")
        self.assertTrue(DeviceStatus.objects.get(device="doorbell").online)
        self.send("device_offline", device="doorbell")
        self.assertFalse(DeviceStatus.objects.get(device="doorbell").online)

    def test_bare_hex_and_wrong_signatures(self):
        from watch import ringapi
        body = json.dumps(ringapi.build_event("ava1.ring.account.TEST", "motion_detected", "k")).encode()
        r = self.client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_SIGNATURE=sign(body)[7:])
        self.assertEqual(r.status_code, 204)               # bare hex digest tolerated
        r = self.client.post("/webhooks/ring/", body + b" ", content_type="application/json", HTTP_X_SIGNATURE=sign(body))
        self.assertEqual(r.status_code, 403)               # signature covers the exact raw bytes

    def test_unknown_account_and_stale_meta_time(self):
        from watch import ringapi
        body = json.dumps(ringapi.build_event("ava1.ring.account.OTHER", "motion_detected", "k")).encode()
        self.assertEqual(self.client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_SIGNATURE=sign(body)).status_code, 400)
        old = timezone.now() - timedelta(hours=3)
        _, r = self.send("motion_detected", now=old)
        self.assertEqual(r.status_code, 400)

    def test_duplicate_delivery_is_idempotent(self):
        from watch import ringapi
        ev = ringapi.build_event("ava1.ring.account.TEST", "motion_detected", "k", at=timezone.now())
        body = json.dumps(ev).encode()
        for want in (204, 204):
            self.assertEqual(self.client.post("/webhooks/ring/", body, content_type="application/json", HTTP_X_SIGNATURE=sign(body)).status_code, want)
        self.assertEqual(Event.objects.count(), 1)

    def test_parse_handles_nanosecond_meta_time_and_battery_status(self):
        from watch import ringapi
        d = ringapi.parse_time("2026-02-13T13:39:57.200155525Z")
        self.assertEqual((d.year, d.second, d.microsecond), (2026, 57, 200155))
        self.assertEqual(ringapi.RingClient.battery_and_online({"online": True, "battery_status": {"percentage": 7}}), (7, True))
        self.assertEqual(ringapi.RingClient.battery_and_online({"online": True, "battery_status": {"percentage": 255}}), (None, True))


class StatusPollTests(Base):
    def test_poll_updates_battery_and_online_from_status_endpoint(self):
        DeviceStatus.objects.create(household=self.h, device="doorbell", last_seen=timezone.now(), battery=90, online=True)
        DeviceStatus.objects.create(household=self.h, device="chime", last_seen=timezone.now(), battery=50, online=True)

        class Fake:
            def status(self, device_id):
                return {"doorbell": {"online": True, "battery_status": {"percentage": 12}},
                        "chime": {"online": False}}[device_id]            # chimes report no battery_status

        self.assertEqual(tasks.poll_ring_status(client=Fake()), {"polled": 2})
        self.assertEqual(DeviceStatus.objects.get(device="doorbell").battery, 12)
        d = DeviceStatus.objects.get(device="chime")
        self.assertEqual((d.battery, d.online), (50, False))

    def test_poll_without_token_is_a_noop(self):
        self.assertEqual(tasks.poll_ring_status()["polled"], 0)


@override_settings(RING_SIMULATOR=False)
class SimulatorGateTests(TestCase):
    def test_simulator_is_disabled_unless_enabled(self):
        from watch import simulator
        from django.test import RequestFactory
        from django.http import Http404
        with self.assertRaises(Http404):
            simulator.page(RequestFactory().get("/simulator/"))
