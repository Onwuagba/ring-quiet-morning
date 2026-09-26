import json
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from watch import channels, escalation, notify, tasks, telegram as tg, tgbot
from watch.models import CheckIn, EmergencyContact, Notification, Profile, TelegramLink

from .base import Base, at, seed_history

MEM = "django.core.mail.backends.locmem.EmailBackend"
SECRET = "s3cret_-token"
TOKEN = "123456:BOT-TOKEN-VALUE"


class FakeTG:
    """Records Bot API calls instead of touching the network."""
    def __init__(self):
        self.calls, self.n = [], 500

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        self.n += 1
        return {"message_id": self.n} if method == "sendMessage" else True

    def sent(self, chat=None):
        return [p for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@override_settings(EMAIL_BACKEND=MEM, TELEGRAM_BOT_USERNAME="QuietMorningBot", TELEGRAM_WEBHOOK_SECRET=SECRET)
class TGBase(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeTG()
        tg.set_transport(self.fake)
        self.addCleanup(tg.set_transport, None)
        seed_history(self.h)
        self.t0 = at(9, 0)

    def tick(self, when):
        with mock.patch("django.utils.timezone.now", return_value=when):
            return tasks.tick()

    def post(self, update, secret=SECRET):
        h = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": secret} if secret is not None else {}
        return self.client.post("/webhooks/telegram/", json.dumps(update), content_type="application/json", **h)

    def press(self, user_chat, notification, action):
        return self.post({"callback_query": {"id": "cb1", "from": {"id": user_chat}, "data": f"{action}:{notification.ack_token.hex}",
                                             "message": {"message_id": notification.telegram_message_id, "chat": {"id": user_chat}}}})

    def open_to_tunde(self):
        self.tick(self.t0)
        self.tick(self.t0 + timedelta(minutes=20))
        return Notification.objects.get(recipient="Tunde", channel="telegram")


class LinkingTests(TGBase):
    def start(self, token, chat=777, chat_type="private"):
        return self.post({"message": {"text": f"/start {token}", "chat": {"id": chat, "type": chat_type}}})

    def test_dashboard_link_then_start_binds_chat(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "me"})
        self.assertContains(r, "https://t.me/QuietMorningBot?start=")
        link = TelegramLink.objects.get(user=self.amina)
        self.assertLessEqual(len(link.token), 64)
        self.assertEqual(self.start(link.token).status_code, 200)
        self.assertEqual(Profile.objects.get(user=self.amina).telegram_chat_id, 777)
        self.assertIn("Connected", self.fake.sent(777)[0]["text"])

    def test_token_is_one_time(self):
        link = tgbot.new_link(user=self.amina)
        self.start(link.token, chat=1)
        self.start(link.token, chat=2)
        self.assertEqual(Profile.objects.get(user=self.amina).telegram_chat_id, 1)
        self.assertIn("expired", self.fake.sent(2)[0]["text"])

    def test_expired_and_unknown_tokens_do_nothing(self):
        link = tgbot.new_link(user=self.amina)
        TelegramLink.objects.filter(pk=link.pk).update(expires_at=at(1, 0, days_ago=2))
        self.start(link.token, chat=5)
        self.start("nonsense", chat=6)
        self.assertNotEqual(Profile.objects.get(user=self.amina).telegram_chat_id, 5)

    def test_group_chats_cannot_link(self):
        link = tgbot.new_link(user=self.amina)
        self.start(link.token, chat=-100, chat_type="group")
        self.assertTrue(TelegramLink.objects.get(pk=link.pk).usable)

    def test_emergency_contact_and_person_links(self):
        self.ec.telegram_chat_id = None
        self.ec.save()
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "contact", "id": self.ec.pk})
        self.assertContains(r, "t.me/QuietMorningBot?start=")
        self.start(TelegramLink.objects.get(contact=self.ec).token, chat=888)
        self.ec.refresh_from_db()
        self.assertEqual(self.ec.telegram_chat_id, 888)
        self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "person"})
        self.start(TelegramLink.objects.get(household=self.h).token, chat=889)
        self.h.refresh_from_db()
        self.assertEqual(self.h.person_telegram_chat_id, 889)

    def test_caregiver_cannot_make_links_for_others(self):
        self.login(self.tunde)
        self.assertEqual(self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "person"}).status_code, 404)
        self.assertEqual(self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "contact", "id": self.ec.pk}).status_code, 404)

    @override_settings(TELEGRAM_BOT_USERNAME="")
    def test_link_needs_bot_username(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/telegram/", {"who": "me"}, follow=True)
        self.assertContains(r, "not set up")
        self.assertFalse(TelegramLink.objects.exists())

    def test_stop_disconnects(self):
        self.post({"message": {"text": "/stop", "chat": {"id": 1002, "type": "private"}}})
        self.assertIsNone(Profile.objects.get(user=self.amina).telegram_chat_id)


class WebhookSecurityTests(TGBase):
    def test_wrong_missing_and_unset_secret_rejected(self):
        u = {"message": {"text": "/start x", "chat": {"id": 1, "type": "private"}}}
        self.assertEqual(self.post(u, secret="wrong").status_code, 403)
        self.assertEqual(self.post(u, secret=None).status_code, 403)
        with override_settings(TELEGRAM_WEBHOOK_SECRET=""):
            self.assertEqual(self.post(u, secret="").status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_bad_secret_never_reaches_handler(self):
        with mock.patch("watch.tgbot.handle_update") as h:
            self.post({"message": {}}, secret="nope")
            h.assert_not_called()

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get("/webhooks/telegram/").status_code, 405)

    def test_garbage_body_and_handler_errors_still_ack(self):
        r = self.client.post("/webhooks/telegram/", "not json", content_type="application/json",
                             HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET)
        self.assertEqual(r.status_code, 400)
        with mock.patch("watch.tgbot._on_callback", side_effect=RuntimeError("boom")):
            self.assertEqual(self.post({"callback_query": {}}).status_code, 200)


class CallbackTests(TGBase):
    def test_send_has_buttons_and_html_mode(self):
        n = self.open_to_tunde()
        msg = self.fake.sent(1003)[0]
        self.assertEqual(msg["parse_mode"], "HTML")
        row = msg["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"].split(" ", 1)[1] for b in row], ["All OK", "Need help"])
        for b in row:
            self.assertLessEqual(len(b["callback_data"].encode()), tg.CALLBACK_MAX_BYTES)
        self.assertEqual(n.status, "sent")
        self.assertTrue(n.telegram_message_id)

    def test_all_ok_confirms_answers_edits_and_informs_others(self):
        n = self.open_to_tunde()
        self.assertEqual(self.press(1003, n, "ok").status_code, 200)
        c = CheckIn.objects.get()
        self.assertEqual((c.state, c.resolution, c.acknowledged_by), ("acknowledged", "all_ok", "Tunde"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[0]["callback_query_id"], "cb1")
        edit = self.fake.of("editMessageText")[0]
        self.assertEqual((edit["chat_id"], edit["message_id"]), (1003, n.telegram_message_id))
        self.assertIn("confirmed Mama is OK", edit["text"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})
        # Amina was messaged earlier over Telegram? Her Telegram was sent (no quiet hours), so she gets the follow-up.
        self.assertTrue(any("confirmed Mama is OK" in p["text"] for p in self.fake.sent(1002)))
        self.assertFalse(any(p["chat_id"] == 1003 and "No further action" in p["text"] for p in self.fake.sent()))
        self.assertFalse(Notification.objects.filter(recipient="Aunty").exists())

    def test_need_help_escalates_like_the_dashboard(self):
        n = self.open_to_tunde()
        self.press(1003, n, "help")
        c = CheckIn.objects.get()
        self.assertEqual((c.state, c.resolution), ("help_requested", "needs_help"))
        self.assertTrue(Notification.objects.filter(recipient="Aunty", channel="telegram", status="sent").exists())
        self.assertIn("asked for help", self.fake.of("editMessageText")[0]["text"])

    def test_callbacks_are_idempotent(self):
        n = self.open_to_tunde()
        self.press(1003, n, "ok")
        before = Notification.objects.count()
        self.press(1003, n, "ok")
        self.press(1003, n, "ok")
        self.assertEqual(Notification.objects.count(), before)
        self.assertEqual(len(self.fake.of("answerCallbackQuery")), 3)   # every tap is answered so the spinner stops
        n2 = self.open_help()
        before = Notification.objects.count()
        self.press(1003, n2, "help")
        self.assertEqual(Notification.objects.count(), before)

    def open_help(self):
        c = CheckIn.objects.get()
        c.state, c.resolution, c.acknowledged_by = "help_requested", "needs_help", "Amina"
        c.save()
        return Notification.objects.get(recipient="Tunde", channel="telegram")

    def test_button_from_another_chat_is_refused(self):
        n = self.open_to_tunde()
        self.press(4242, n, "ok")
        self.assertEqual(CheckIn.objects.get().state, "open")
        self.assertTrue(self.fake.of("answerCallbackQuery")[0]["show_alert"])
        self.assertEqual(self.fake.of("editMessageText"), [])

    def test_malformed_callback_data_is_harmless(self):
        for data in ("", "ok", "ok:zzz", "boom:" + "0" * 32, "ok:" + "0" * 32):
            self.post({"callback_query": {"id": "x", "from": {"id": 1003}, "data": data}})
        self.assertEqual(CheckIn.objects.count(), 0)

    def test_nudge_button_records_person_ok(self):
        self.h.nudge_enabled = True
        self.h.save()
        self.tick(self.t0)
        n = Notification.objects.get(recipient="Mama", channel="telegram")
        self.press(1001, n, "ok")
        self.assertEqual(CheckIn.objects.get().resolution, "person_ok")


class PrivacyAndFormatTests(TGBase):
    def test_chat_id_not_in_audit_rows_pages_or_export(self):
        # distinctive ids so a hex UUID cannot collide by accident
        ids = {1001: 987650001, 1002: 987650002, 1003: 987650003, 1004: 987650004}
        self.h.person_telegram_chat_id = ids[1001]
        self.h.save()
        Profile.objects.filter(user=self.amina).update(telegram_chat_id=ids[1002])
        Profile.objects.filter(user=self.tunde).update(telegram_chat_id=ids[1003])
        EmergencyContact.objects.filter(pk=self.ec.pk).update(telegram_chat_id=ids[1004])
        n = self.open_to_tunde()
        self.press(ids[1003], n, "ok")
        blob = " ".join(f"{x.destination} {x.detail} {x.text} {x.recipient}" for x in Notification.objects.all())
        self.login()
        pages = [self.client.get(u).content.decode() for u in (
            f"/h/{self.h.pk}/", f"/h/{self.h.pk}/history/", f"/h/{self.h.pk}/me/", f"/h/{self.h.pk}/team/",
            f"/h/{self.h.pk}/settings/", f"/h/{self.h.pk}/export/")]
        for chat in map(str, ids.values()):
            self.assertNotIn(chat, blob)
            for p in pages:
                self.assertNotIn(f'"{chat}"', p)
                self.assertNotIn(f">{chat}<", p)
                self.assertNotIn(f"chat_id", p)

    def test_chat_id_not_in_logs(self):
        with self.assertLogs("watch.channels", level="INFO") as cm:
            tg.set_transport(None)
            with override_settings(TELEGRAM_BOT_TOKEN=""):
                channels.telegram(1003, "x")
        self.assertNotIn("1003", " ".join(cm.output))

    def test_html_escaping_and_length(self):
        self.assertEqual(tg.escape("a < b & c > d <b>x</b>"), "a &lt; b &amp; c &gt; d &lt;b&gt;x&lt;/b&gt;")
        long = "<&>" * 3000
        out = tg.html_text(long)
        self.assertLessEqual(len(out), tg.MAX_TEXT)
        self.assertNotRegex(out, r"&(?!amp;|lt;|gt;)")      # nothing cut mid-entity
        self.assertNotIn("<", out)

    def test_hostile_household_name_is_escaped_in_sent_message(self):
        self.h.name = "Mama <b>&</b>"
        self.h.save()
        self.open_to_tunde()
        text = self.fake.sent(1003)[0]["text"]
        self.assertIn("Mama &lt;b&gt;&amp;&lt;/b&gt;", text)
        self.assertNotIn("<b>", text)

    def test_real_http_request_shape_and_token_scrubbed(self):
        tg.set_transport(None)
        with override_settings(TELEGRAM_BOT_TOKEN=TOKEN):
            with mock.patch("requests.post") as post:
                post.return_value.json.return_value = {"ok": True, "result": {"message_id": 9}}
                r = channels.telegram(1003, "hi <x>", ack_token="6f1c2d3e-0000-4000-8000-000000000001")
            self.assertEqual(r, channels.Result("sent", "telegram delivered", 9))
            self.assertEqual(post.call_args.args[0], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            self.assertEqual(post.call_args.kwargs["json"]["text"], "hi &lt;x&gt;")
            # network failure: requests puts the URL (with the token) in its message; we must not
            with mock.patch("requests.post", side_effect=requests.ConnectionError(f"boom https://api.telegram.org/bot{TOKEN}/sendMessage")):
                r = channels.telegram(1003, "hi")
            self.assertEqual(r.status, "error")
            self.assertNotIn(TOKEN, r.detail)
            # API error: 429 carries retry_after, description is scrubbed too
            with mock.patch("requests.post") as post:
                post.return_value.status_code = 429
                post.return_value.json.return_value = {"ok": False, "error_code": 429, "description": f"Too Many {TOKEN}",
                                                       "parameters": {"retry_after": 7}}
                r = channels.telegram(1003, "hi")
            self.assertIn("retry after 7s", r.detail)
            self.assertNotIn(TOKEN, r.detail)

    def test_no_token_stubs_and_logs_nothing_sensitive(self):
        tg.set_transport(None)
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            self.assertEqual(channels.telegram(1003, "hi").status, "stubbed")


class DeliveryRulesTests(TGBase):
    def test_quiet_hours_defer_telegram(self):
        self.ma.quiet_start, self.ma.quiet_end = at(6).time(), at(10).time()
        self.ma.save()
        self.h.nudge_enabled = False
        self.h.save()
        self.tick(self.t0)
        rows = {n.channel: n.status for n in Notification.objects.filter(recipient="Amina")}
        self.assertEqual(rows["telegram"], "deferred")
        self.assertEqual(self.fake.sent(1002), [])

    def test_no_follow_up_over_telegram_for_deferred_message(self):
        self.ma.quiet_start, self.ma.quiet_end = at(6).time(), at(10).time()
        self.ma.save()
        n = self.open_to_tunde()   # by step 3, urgent ignores quiet hours for Tunde only; Amina's was deferred
        self.press(1003, n, "ok")
        self.assertFalse(any("confirmed" in p["text"] for p in self.fake.sent(1002)))

    def test_unlinked_caregiver_is_skipped_and_audited(self):
        Profile.objects.filter(user=self.tunde).update(telegram_chat_id=None)
        self.h.nudge_enabled = False
        self.h.save()
        self.tick(self.t0)
        self.tick(self.t0 + timedelta(minutes=20))
        row = Notification.objects.get(recipient="Tunde", channel="telegram")
        self.assertEqual(row.status, "skipped")

    def test_failure_retries_with_backoff(self):
        boom = mock.Mock(side_effect=tg.TelegramError("Bad Gateway", 502))
        tg.set_transport(lambda m, p: boom())
        self.h.nudge_enabled = False
        self.h.save()
        self.tick(self.t0)
        n = Notification.objects.get(recipient="Amina", channel="telegram")
        self.assertEqual((n.status, n.attempts), ("retrying", 1))
        self.assertEqual(n.next_retry_at, self.t0 + timedelta(minutes=1))
        notify.retry_due(self.t0 + timedelta(minutes=2))
        n.refresh_from_db()
        self.assertEqual(n.attempts, 2)


class CommandTests(TGBase):
    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_set_webhook_sends_secret_and_requires_https(self):
        call_command("telegram_bot", set_webhook="https://qm.example.com/", stdout=StringIO())
        p = self.fake.of("setWebhook")[0]
        self.assertEqual(p["url"], "https://qm.example.com/webhooks/telegram/")
        self.assertEqual(p["secret_token"], SECRET)
        self.assertEqual(p["allowed_updates"], ["message", "callback_query"])
        with self.assertRaises(CommandError):
            call_command("telegram_bot", set_webhook="http://localhost:8000", stdout=StringIO())
        with override_settings(TELEGRAM_WEBHOOK_SECRET=""):
            with self.assertRaises(CommandError):
                call_command("telegram_bot", set_webhook="https://x.example.com", stdout=StringIO())

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_poll_once_deletes_webhook_and_dispatches_updates(self):
        link = tgbot.new_link(user=self.amina)
        updates = [{"update_id": 41, "message": {"text": f"/start {link.token}", "chat": {"id": 31337, "type": "private"}}}]
        base = self.fake

        def transport(method, payload):
            if method == "getUpdates":
                base.calls.append((method, payload))
                return updates
            return base(method, payload)
        tg.set_transport(transport)
        call_command("telegram_bot", poll=True, once=True, stdout=StringIO())
        self.assertEqual(len(self.fake.of("deleteWebhook")), 1)
        self.assertEqual(Profile.objects.get(user=self.amina).telegram_chat_id, 31337)

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_command_needs_token(self):
        tg.set_transport(None)
        with self.assertRaises(CommandError):
            call_command("telegram_bot", info=True, stdout=StringIO())
