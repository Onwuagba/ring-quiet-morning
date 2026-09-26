from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from watch import channels, escalation, notify, rules, tasks
from watch.models import CheckIn, Event, Notification

from .base import Base, at, seed_history, today

MEM = "django.core.mail.backends.locmem.EmailBackend"


@override_settings(EMAIL_BACKEND=MEM)
class ChainTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h)
        self.h.nudge_enabled = True
        self.h.save()
        for m in (self.ma, self.mt):
            m.notify_sms = True
            m.save()
        self.t0 = at(9, 0)

    def tick(self, when):
        with mock.patch("django.utils.timezone.now", return_value=when):
            return tasks.tick()

    def test_chain_order_nudge_caregivers_emergency(self):
        chain = escalation.build_chain(self.h)
        self.assertEqual([s.kind for s in chain], ["nudge", "caregiver", "caregiver", "emergency"])
        self.assertEqual(chain[1].label, "Amina")   # priority 1 first

    def test_no_nudge_without_phone_or_flag(self):
        self.h.nudge_enabled = False
        self.h.save()
        self.assertEqual(escalation.build_chain(self.h)[0].kind, "caregiver")

    def test_full_escalation_when_nobody_answers(self):
        self.assertEqual(self.tick(self.t0)["opened"], 1)
        c = CheckIn.objects.get()
        self.assertEqual((c.level, c.state), (1, "open"))
        self.tick(self.t0 + timedelta(minutes=5))          # too soon
        self.assertEqual(CheckIn.objects.get().level, 1)
        self.tick(self.t0 + timedelta(minutes=10))         # nudge delay is 10
        self.tick(self.t0 + timedelta(minutes=30))
        self.tick(self.t0 + timedelta(minutes=50))
        c.refresh_from_db()
        self.assertEqual(c.level, 4)
        recipients = list(Notification.objects.filter(channel="email").values_list("recipient", flat=True))
        self.assertIn("Amina", recipients)
        self.assertIn("Tunde", recipients)
        self.assertIn("Aunty", recipients)
        self.tick(self.t0 + timedelta(minutes=80))
        self.assertEqual(CheckIn.objects.get().state, "exhausted")

    def test_ack_stops_chain_and_informs_others(self):
        self.tick(self.t0)
        self.tick(self.t0 + timedelta(minutes=10))
        c = CheckIn.objects.get()
        mail.outbox.clear()
        escalation.acknowledge(c, "all_ok", user=self.amina, now=self.t0 + timedelta(minutes=12))
        c.refresh_from_db()
        self.assertEqual((c.state, c.resolution, c.acknowledged_by), ("acknowledged", "all_ok", "Amina"))
        self.tick(self.t0 + timedelta(minutes=90))
        self.assertFalse(Notification.objects.filter(recipient="Tunde").exists())
        self.assertFalse(Notification.objects.filter(recipient="Aunty").exists())
        self.assertEqual(CheckIn.objects.count(), 1)   # no second alert same day

    def test_needs_help_alerts_everyone_now_ignoring_quiet_hours(self):
        self.mt.quiet_start, self.mt.quiet_end = at(0).time(), at(23, 59).time()
        self.mt.save()
        self.tick(self.t0)
        c = CheckIn.objects.get()
        escalation.acknowledge(c, "needs_help", user=self.amina, now=self.t0 + timedelta(minutes=1))
        c.refresh_from_db()
        self.assertEqual(c.state, "help_requested")
        self.assertTrue(Notification.objects.filter(recipient="Tunde", channel="sms", status="stubbed").exists())
        self.assertTrue(Notification.objects.filter(recipient="Aunty").exists())

    def test_ack_by_token_from_message_link(self):
        self.tick(self.t0)
        n = Notification.objects.filter(recipient="Mama").first()
        escalation.acknowledge_by_token(n.ack_token, "person_ok", now=self.t0)
        c = CheckIn.objects.get()
        self.assertEqual((c.state, c.resolution), ("acknowledged", "person_ok"))

    def test_quiet_hours_defer_phone_but_email_goes(self):
        self.h.nudge_enabled = False
        self.h.save()
        self.ma.quiet_start, self.ma.quiet_end = at(6).time(), at(10).time()
        self.ma.save()
        self.tick(self.t0)
        rows = {n.channel: n.status for n in Notification.objects.filter(recipient="Amina")}
        self.assertEqual(rows["email"], "sent")
        self.assertEqual(rows["sms"], "deferred")

    def test_quiet_hours_wrap_midnight(self):
        self.ma.quiet_start, self.ma.quiet_end = at(22).time(), at(7).time()
        self.assertTrue(notify.in_quiet_hours(self.ma, at(23)))
        self.assertTrue(notify.in_quiet_hours(self.ma, at(3)))
        self.assertFalse(notify.in_quiet_hours(self.ma, at(12)))

    def test_step_with_no_channels_advances_immediately(self):
        self.h.nudge_enabled = False
        self.h.save()
        for f in ("notify_email", "notify_push", "notify_sms", "notify_whatsapp"):
            setattr(self.ma, f, False)
        self.ma.save()
        self.tick(self.t0)
        c = CheckIn.objects.get()
        self.assertEqual(c.level, 2)     # Amina had nothing enabled, Tunde was contacted straight away
        self.assertTrue(Notification.objects.filter(recipient="Amina", status="skipped").exists())
        self.assertTrue(Notification.objects.filter(recipient="Tunde", channel="email", status="sent").exists())

    def test_role_viewer_gets_no_alerts(self):
        self.mt.role = "viewer"
        self.mt.save()
        self.assertEqual([s.label for s in escalation.build_chain(self.h) if s.kind == "caregiver"], ["Amina"])

    def test_sensor_notice_goes_to_owner_and_first_only(self):
        Event.objects.all().delete()
        seed_history(self.h, days=8)
        Event.objects.filter(at__gte=at(0, 0, days_ago=4)).delete()
        self.assertEqual(self.tick(self.t0)["opened"], 1)
        c = CheckIn.objects.get()
        self.assertEqual(c.kind, "sensor")
        self.assertFalse(Notification.objects.filter(recipient="Aunty").exists())
        self.assertFalse(Notification.objects.filter(recipient="Mama").exists())
        self.assertTrue(Notification.objects.filter(recipient="Amina").exists())
        self.tick(self.t0 + timedelta(minutes=1))
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_away_and_snooze_prevent_alert(self):
        self.h.snooze_until = self.t0 + timedelta(hours=1)
        self.h.save()
        self.assertEqual(self.tick(self.t0)["opened"], 0)


@override_settings(EMAIL_BACKEND=MEM)
class RetryTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h)
        self.t0 = at(9, 0)

    def test_failure_backoff_then_success(self):
        seq = [channels.Result("error", "boom"), channels.Result("error", "boom"), channels.Result("sent", "ok")]
        with mock.patch("watch.channels.email", side_effect=seq):
            with mock.patch("django.utils.timezone.now", return_value=self.t0):
                tasks.tick()
            n = Notification.objects.get(recipient="Amina", channel="email")
            self.assertEqual((n.status, n.attempts), ("retrying", 1))
            self.assertEqual(n.next_retry_at, self.t0 + timedelta(minutes=1))
            self.assertEqual(notify.retry_due(self.t0 + timedelta(seconds=30)), 0)   # backoff not elapsed
            notify.retry_due(self.t0 + timedelta(minutes=1))
            n.refresh_from_db()
            self.assertEqual((n.status, n.attempts), ("retrying", 2))
            self.assertEqual(n.next_retry_at, self.t0 + timedelta(minutes=6))       # 5 min backoff
            notify.retry_due(self.t0 + timedelta(minutes=7))
            n.refresh_from_db()
            self.assertEqual((n.status, n.attempts), ("sent", 3))

    def test_gives_up_after_max_attempts_and_exception_is_contained(self):
        with mock.patch("watch.channels.email", side_effect=RuntimeError("smtp down")):
            with mock.patch("django.utils.timezone.now", return_value=self.t0):
                tasks.tick()
            t = self.t0
            for _ in range(5):
                t += timedelta(minutes=20)
                notify.retry_due(t)
        n = Notification.objects.get(recipient="Amina", channel="email")
        self.assertEqual((n.status, n.attempts), ("failed", 4))
        self.assertIn("gave up", n.detail)

    def test_retry_skipped_when_checkin_already_acknowledged(self):
        with mock.patch("watch.channels.email", return_value=channels.Result("error", "x")):
            with mock.patch("django.utils.timezone.now", return_value=self.t0):
                tasks.tick()
        escalation.acknowledge(CheckIn.objects.get(), "all_ok", user=self.amina, now=self.t0)
        notify.retry_due(self.t0 + timedelta(minutes=2))
        self.assertEqual(Notification.objects.get(recipient="Amina", channel="email").status, "skipped")

    def test_one_broken_channel_does_not_block_others(self):
        self.ma.notify_sms = True
        self.ma.save()
        with mock.patch("watch.channels.sms", side_effect=RuntimeError("x")):
            with mock.patch("django.utils.timezone.now", return_value=self.t0):
                tasks.tick()
        st = {n.channel: n.status for n in Notification.objects.filter(recipient="Amina")}
        self.assertEqual(st["email"], "sent")
        self.assertEqual(st["sms"], "retrying")


class ChannelTests(Base):
    @override_settings(SMS_ENABLED=True, TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_SMS_FROM="")
    def test_sms_stub_without_keys(self):
        self.assertEqual(channels.sms("+234", "hi").status, "stubbed")

    @override_settings(SMS_ENABLED=False)
    def test_sms_disabled_by_flag(self):
        self.assertEqual(channels.sms("+234", "hi").status, "stubbed")

    @override_settings(WHATSAPP_ENABLED=True, TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", TWILIO_WHATSAPP_FROM="+1415")
    def test_whatsapp_posts_to_twilio(self):
        with mock.patch("requests.post") as post:
            post.return_value.ok, post.return_value.status_code = True, 201
            r = channels.whatsapp("+234999", "hello")
        self.assertEqual(r.status, "sent")
        self.assertEqual(post.call_args.kwargs["data"]["To"], "whatsapp:+234999")
        self.assertIn("AC1", post.call_args.args[0])

    @override_settings(SMS_ENABLED=True, TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", TWILIO_SMS_FROM="+1415")
    def test_twilio_http_error_is_reported(self):
        with mock.patch("requests.post") as post:
            post.return_value.ok, post.return_value.status_code, post.return_value.text = False, 400, "bad"
            self.assertEqual(channels.sms("+234", "x").status, "error")

    @override_settings(VAPID_PRIVATE_KEY="", VAPID_PUBLIC_KEY="")
    def test_push_stub_and_no_subscription(self):
        self.assertEqual(channels.push(self.amina, "t", "b").status, "error")
        self.amina.push_subscriptions.create(endpoint="https://push.example/1", p256dh="k", auth="a")
        self.assertEqual(channels.push(self.amina, "t", "b").status, "stubbed")

    @override_settings(VAPID_PRIVATE_KEY="priv", VAPID_PUBLIC_KEY="pub")
    def test_push_sends_via_pywebpush(self):
        self.amina.push_subscriptions.create(endpoint="https://push.example/1", p256dh="k", auth="a")
        with mock.patch("pywebpush.webpush") as wp:
            r = channels.push(self.amina, "t", "b", url="/x")
        self.assertEqual(r.status, "sent")
        self.assertEqual(wp.call_args.args[0]["endpoint"], "https://push.example/1")

    def test_mask(self):
        self.assertEqual(channels.mask("+2348012345678"), "**********5678")
        self.assertEqual(channels.mask("amina@example.com"), "a***@example.com")
