import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from watch import escalation, rules
from watch.models import (CheckIn, EmergencyContact, Event, ExpectedLate, Household, Invite, Membership,
                          Notification, Profile)

from .base import PW, Base, at, make_user, seed_history, today

User = get_user_model()
MEM = "django.core.mail.backends.locmem.EmailBackend"


class AuthTests(Base):
    def test_anonymous_redirected_to_login_everywhere(self):
        for name in ("dashboard", "audit", "settings", "team", "prefs", "export", "delete", "summary", "status_json"):
            from django.urls import reverse
            r = self.client.get(reverse(name, args=[self.h.pk]))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/login/", r["Location"])

    def test_landing_for_anonymous_and_login_flow(self):
        self.assertContains(self.client.get("/"), "Create a caregiver account")
        r = self.client.post("/login/", {"username": "amina", "password": PW})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/"), f"/h/{self.h.pk}/")

    def test_wrong_password(self):
        r = self.client.post("/login/", {"username": "amina", "password": "bad"})
        self.assertEqual(r.status_code, 200)

    def test_non_member_gets_404_no_leak(self):
        make_user("stranger")
        self.client.login(username="stranger", password=PW)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/export/").status_code, 404)

    def test_signup_creates_profile_and_goes_to_onboarding(self):
        r = self.client.post("/signup/", {"username": "newbie", "email": "n@example.com", "first_name": "New",
                                          "password1": PW, "password2": PW})
        self.assertRedirects(r, "/onboarding/1/", fetch_redirect_response=False)
        self.assertTrue(Profile.objects.filter(user__username="newbie").exists())

    def test_multiple_households_listed(self):
        h2 = Household.objects.create(name="Papa", ring_location_id="loc2", window_start=at(5).time(), check_by=at(9).time())
        Membership.objects.create(user=self.amina, household=h2, role="owner")
        self.login()
        r = self.client.get("/")
        self.assertContains(r, "Papa")
        self.assertContains(r, "Mama")


class RoleTests(Base):
    def test_viewer_is_read_only(self):
        v = make_user("vera")
        Membership.objects.create(user=v, household=self.h, role="viewer", priority=9)
        self.login(v)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/").status_code, 200)
        self.assertEqual(self.client.post(f"/h/{self.h.pk}/", {"action": "snooze", "minutes": 30}).status_code, 403)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/team/").status_code, 403)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/settings/").status_code, 403)

    def test_caregiver_can_act_but_not_manage(self):
        self.login(self.tunde)
        r = self.client.post(f"/h/{self.h.pk}/", {"action": "snooze", "minutes": 60})
        self.assertEqual(r.status_code, 302)
        self.h.refresh_from_db()
        self.assertIsNotNone(self.h.snooze_until)
        self.assertEqual(self.client.get(f"/h/{self.h.pk}/settings/").status_code, 403)
        self.assertEqual(self.client.post(f"/h/{self.h.pk}/delete/", {"confirm": "Mama"}).status_code, 403)
        self.assertTrue(Household.objects.exists())


class OnboardingTests(Base):
    def test_full_wizard(self):
        u = make_user("newowner")
        self.client.login(username="newowner", password=PW)
        self.assertRedirects(self.client.get("/"), "/onboarding/1/", fetch_redirect_response=False)
        r = self.client.post("/onboarding/1/", {"name": "Grandpa", "ring_location_id": "", "person_phone": "+2341"})
        self.assertRedirects(r, "/onboarding/2/", fetch_redirect_response=False)
        h = Household.objects.get(name="Grandpa")
        self.assertTrue(h.ring_location_id.startswith("qm-"))
        self.assertEqual(Membership.objects.get(user=u, household=h).role, "owner")
        self.assertEqual(self.client.post("/onboarding/2/", {"window_start": "05:30", "check_by": "05:00"}).status_code, 200)  # invalid
        r = self.client.post("/onboarding/2/", {"window_start": "05:30", "check_by": "09:30", "adaptive": "on"})
        self.assertRedirects(r, "/onboarding/3/", fetch_redirect_response=False)
        with override_settings(EMAIL_BACKEND=MEM):
            r = self.client.post("/onboarding/3/", {"me-phone": "+2342", "me-notify_email": "on", "ec-name": "Neighbour",
                                                    "ec-phone": "+2343", "inv-email": "sis@example.com", "inv-role": "caregiver"})
        self.assertRedirects(r, "/onboarding/4/", fetch_redirect_response=False)
        self.assertEqual(h.emergency_contacts.count(), 1)
        self.assertEqual(Invite.objects.get().email, "sis@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/invite/", mail.outbox[0].body)
        r = self.client.post("/onboarding/4/", {})
        self.assertEqual(r.status_code, 200)                       # consent required
        r = self.client.post("/onboarding/4/", {"consent": "1"})
        self.assertRedirects(r, f"/h/{h.pk}/", fetch_redirect_response=False)
        h.refresh_from_db()
        self.assertIsNotNone(h.consent_at)
        self.assertContains(self.client.get(f"/h/{h.pk}/"), "Waiting for the first Ring event")   # empty state

    def test_step_requires_household(self):
        make_user("x1")
        self.client.login(username="x1", password=PW)
        self.assertRedirects(self.client.get("/onboarding/3/"), "/onboarding/1/", fetch_redirect_response=False)

    def test_emergency_contact_needs_a_way_to_reach(self):
        self.login()
        s = self.client.session
        s["onboarding_household"] = self.h.pk
        s.save()
        r = self.client.post("/onboarding/3/", {"me-notify_email": "on", "ec-name": "Nobody"})
        self.assertEqual(r.status_code, 200)

    def test_duplicate_ring_location_rejected(self):
        make_user("y1")
        self.client.login(username="y1", password=PW)
        r = self.client.post("/onboarding/1/", {"name": "Dup", "ring_location_id": "loc1"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Household.objects.filter(name="Dup").exists())


@override_settings(EMAIL_BACKEND=MEM)
class InviteTests(Base):
    def invite(self, **kw):
        return Invite.objects.create(household=self.h, email="new@example.com", invited_by=self.amina,
                                     expires_at=timezone.now() + timedelta(days=7), **kw)

    def test_owner_sends_invite_from_team_page(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/team/", {"action": "invite", "inv-email": "b@example.com", "inv-role": "viewer"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Invite.objects.get().role, "viewer")
        self.assertEqual(mail.outbox[0].to, ["b@example.com"])

    def test_new_user_accepts_invite_and_is_next_in_chain(self):
        inv = self.invite()
        r = self.client.post(f"/invite/{inv.token}/", {"username": "bola", "email": "new@example.com", "first_name": "Bola",
                                                       "password1": PW, "password2": PW})
        self.assertEqual(r.status_code, 302)
        m = Membership.objects.get(user__username="bola")
        self.assertEqual((m.role, m.priority, m.household), ("caregiver", 3, self.h))
        inv.refresh_from_db()
        self.assertIsNotNone(inv.accepted_at)
        self.assertEqual(self.client.get(f"/invite/{inv.token}/").status_code, 410)   # single use

    def test_existing_user_accepts(self):
        inv = self.invite(role="viewer")
        u = make_user("ext")
        self.client.login(username="ext", password=PW)
        self.assertContains(self.client.get(f"/invite/{inv.token}/"), "Accept invitation")
        self.client.post(f"/invite/{inv.token}/")
        self.assertEqual(Membership.objects.get(user=u).role, "viewer")

    def test_expired_invite(self):
        inv = self.invite()
        Invite.objects.filter(pk=inv.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(f"/invite/{inv.token}/").status_code, 410)

    def test_bad_token_404(self):
        self.assertEqual(self.client.get("/invite/00000000-0000-0000-0000-000000000000/").status_code, 404)

    def test_team_management_guards_last_owner(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/team/", {"action": "member", "id": self.ma.pk, "role": "caregiver", "priority": 1})
        self.ma.refresh_from_db()
        self.assertEqual(self.ma.role, "owner")
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "remove_member", "id": self.ma.pk})
        self.assertTrue(Membership.objects.filter(pk=self.ma.pk).exists())
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "member", "id": self.mt.pk, "role": "caregiver", "priority": 1})
        self.mt.refresh_from_db()
        self.assertEqual(self.mt.priority, 1)
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "remove_member", "id": self.mt.pk})
        self.assertFalse(Membership.objects.filter(pk=self.mt.pk).exists())

    def test_cannot_manage_other_households_members(self):
        h2 = Household.objects.create(name="Other", ring_location_id="o", window_start=at(5).time(), check_by=at(9).time())
        other = make_user("otherowner")
        Membership.objects.create(user=other, household=h2, role="owner")
        self.login()
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "remove_member", "id": Membership.objects.get(household=h2).pk})
        self.assertTrue(Membership.objects.filter(household=h2).exists())

    def test_emergency_contact_add_remove(self):
        self.login()
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "contact", "ec-name": "Chidi", "ec-email": "c@example.com"})
        c = EmergencyContact.objects.get(name="Chidi")
        self.client.post(f"/h/{self.h.pk}/team/", {"action": "remove_contact", "id": c.pk})
        self.assertFalse(EmergencyContact.objects.filter(name="Chidi").exists())


@override_settings(EMAIL_BACKEND=MEM)
class DashboardTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h)
        self.login()

    def get(self, now=None):
        if now:
            with mock.patch("django.utils.timezone.now", return_value=now):
                return self.client.get(f"/h/{self.h.pk}/")
        return self.client.get(f"/h/{self.h.pk}/")

    def test_normal_state_and_explanation(self):
        Event.objects.create(household=self.h, kind="motion", device="kitchen", at=at(6, 40))
        r = self.get(at(7))
        self.assertContains(r, "Normal morning")
        self.assertContains(r, "s-ok")
        self.assertContains(r, "Motion")

    def test_quiet_state_with_confidence_and_buttons(self):
        with mock.patch("django.utils.timezone.now", return_value=at(9, 5)):
            from watch import tasks
            tasks.tick()
            r = self.client.get(f"/h/{self.h.pk}/")
        self.assertContains(r, "check-in in progress")
        self.assertContains(r, "Confidence")
        self.assertContains(r, "They need help")

    def test_ack_via_dashboard_records_who(self):
        from watch import tasks
        with mock.patch("django.utils.timezone.now", return_value=at(9, 5)):
            tasks.tick()
        self.client.post(f"/h/{self.h.pk}/", {"action": "all_ok"})
        c = CheckIn.objects.get()
        self.assertEqual((c.state, c.acknowledged_by), ("acknowledged", "Amina"))
        self.assertContains(self.client.get(f"/h/{self.h.pk}/"), "Confirmed OK")

    def test_manual_alert_the_team(self):
        r = self.client.post(f"/h/{self.h.pk}/", {"action": "needs_help"})
        c = CheckIn.objects.get()
        self.assertEqual(c.state, "help_requested")
        self.assertTrue(Notification.objects.filter(recipient="Tunde").exists())
        self.assertContains(self.client.get(f"/h/{self.h.pk}/"), "Help requested")

    def test_all_ok_without_checkin_is_harmless(self):
        self.client.post(f"/h/{self.h.pk}/", {"action": "all_ok"})
        self.assertEqual(CheckIn.objects.count(), 0)

    def test_expected_late_snooze_away_actions(self):
        self.client.post(f"/h/{self.h.pk}/", {"action": "expected_late", "until_time": "11:00", "note": "Clinic"})
        self.assertEqual(ExpectedLate.objects.get().note, "Clinic")
        self.assertContains(self.get(at(9, 30)), "Clinic")
        self.client.post(f"/h/{self.h.pk}/", {"action": "clear_expected_late"})
        self.assertEqual(ExpectedLate.objects.count(), 0)
        self.client.post(f"/h/{self.h.pk}/", {"action": "snooze", "minutes": 120})
        self.h.refresh_from_db()
        self.assertTrue(self.h.is_snoozed())
        self.client.post(f"/h/{self.h.pk}/", {"action": "snooze", "minutes": 0})
        self.h.refresh_from_db()
        self.assertFalse(self.h.is_snoozed())
        self.client.post(f"/h/{self.h.pk}/", {"action": "away_on", "until": str(today() + timedelta(days=5))})
        self.h.refresh_from_db()
        self.assertTrue(self.h.is_away())
        self.assertContains(self.get(), "Away mode")
        self.client.post(f"/h/{self.h.pk}/", {"action": "away_off"})
        self.h.refresh_from_db()
        self.assertFalse(self.h.is_away())

    def test_invalid_snooze_minutes_ignored(self):
        self.client.post(f"/h/{self.h.pk}/", {"action": "snooze", "minutes": 99999})
        self.h.refresh_from_db()
        self.assertIsNone(self.h.snooze_until)

    def test_sensor_state_rendered(self):
        Event.objects.filter(at__gte=at(0, 0, days_ago=4)).delete()
        r = self.get(at(9, 0))
        self.assertContains(r, "Device problem")
        self.assertContains(r, "Offline")

    def test_drift_card_and_ribbon_render(self):
        Event.objects.all().delete()
        for i in range(1, 57):
            m = 6 * 60 + 30 + (60 if i <= 12 else 0)
            Event.objects.create(household=self.h, kind="motion", at=at(m // 60, m % 60, days_ago=i))
        r = self.get(at(12))
        self.assertContains(r, "Gentle early warning")
        self.assertContains(r, "<svg")
        self.assertContains(r, 'class="ribbon"')

    def test_timeline_shows_notifications_and_ack(self):
        from watch import tasks
        with mock.patch("django.utils.timezone.now", return_value=at(9, 5)):
            tasks.tick()
        self.client.post(f"/h/{self.h.pk}/", {"action": "all_ok"})
        r = self.get()
        self.assertContains(r, "Quiet-morning check-in opened")
        self.assertContains(r, "all ok")

    def test_status_json_and_history_and_summary_pages(self):
        self.assertIn("state", self.client.get(f"/h/{self.h.pk}/status.json").json())
        self.assertContains(self.client.get(f"/h/{self.h.pk}/history/"), "No check-ins yet")
        self.assertContains(self.client.get(f"/h/{self.h.pk}/summary/"), "Your week with Mama")

    def test_history_lists_audit_rows(self):
        from watch import tasks
        with mock.patch("django.utils.timezone.now", return_value=at(9, 5)):
            tasks.tick()
        r = self.client.get(f"/h/{self.h.pk}/history/")
        self.assertContains(r, "Amina")
        self.assertContains(r, "sent")

    def test_empty_household_renders(self):
        h = Household.objects.create(name="Fresh", ring_location_id="f", window_start=at(5).time(), check_by=at(9).time())
        Membership.objects.create(user=self.amina, household=h, role="owner")
        r = self.client.get(f"/h/{h.pk}/")
        self.assertContains(r, "Waiting for the first Ring event")
        self.assertContains(r, "Still learning")


class PrefsAndPwaTests(Base):
    def test_prefs_saved_and_large_type_applied(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/me/", {"phone": "+2349", "large_type": "on", "notify_sms": "on",
                                                    "notify_email": "on", "quiet_start": "22:00", "quiet_end": "07:00"})
        self.assertEqual(r.status_code, 302)
        self.ma.refresh_from_db()
        self.assertTrue(self.ma.notify_sms and not self.ma.notify_push)
        self.assertEqual(str(self.ma.quiet_start), "22:00:00")
        self.assertContains(self.client.get(f"/h/{self.h.pk}/"), 'class="large"')

    def test_prefs_are_per_caregiver(self):
        self.login(self.tunde)
        self.client.post(f"/h/{self.h.pk}/me/", {"notify_whatsapp": "on"})
        self.ma.refresh_from_db()
        self.mt.refresh_from_db()
        self.assertTrue(self.mt.notify_whatsapp)
        self.assertFalse(self.ma.notify_whatsapp)

    def test_toggle_large_type(self):
        self.login()
        self.client.post("/me/large-type/", {"next": f"/h/{self.h.pk}/"})
        self.assertTrue(Profile.objects.get(user=self.amina).large_type)
        self.client.post("/me/large-type/", {"next": "https://evil.example/"})   # no open redirect
        self.assertFalse(Profile.objects.get(user=self.amina).large_type)

    def test_manifest_is_installable_shape(self):
        m = self.client.get("/manifest.webmanifest").json()
        self.assertEqual(m["display"], "standalone")
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertEqual(sizes, {"192x192", "512x512"})
        for i in m["icons"]:
            from django.contrib.staticfiles import finders
            self.assertIsNotNone(finders.find(i["src"].replace("/static/", "", 1)))

    def test_service_worker_scope_and_content(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r["Service-Worker-Allowed"], "/")
        self.assertIn("javascript", r["Content-Type"])
        self.assertIn("showNotification", r.content.decode())

    def test_push_subscribe(self):
        self.login()
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
        r = self.client.post("/push/subscribe/", json.dumps(sub), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.amina.push_subscriptions.count(), 1)
        self.assertEqual(self.client.post("/push/subscribe/", "{}", content_type="application/json").status_code, 400)
        bad = dict(sub, endpoint="http://insecure.example/x")
        self.assertEqual(self.client.post("/push/subscribe/", json.dumps(bad), content_type="application/json").status_code, 400)

    def test_push_requires_login(self):
        self.assertEqual(self.client.post("/push/subscribe/", "{}", content_type="application/json").status_code, 302)


class AckLinkTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h)

    def open_checkin(self):
        from watch import tasks
        with mock.patch("django.utils.timezone.now", return_value=at(9, 5)), override_settings(EMAIL_BACKEND=MEM):
            tasks.tick()
        return Notification.objects.filter(recipient="Amina", channel="email").first()

    def test_link_page_needs_no_login_and_confirms(self):
        n = self.open_checkin()
        url = f"/ack/{n.ack_token}/"
        self.assertContains(self.client.get(url), "Quiet morning check-in")
        with override_settings(EMAIL_BACKEND=MEM):
            r = self.client.post(url, {"action": "all_ok"})
        self.assertContains(r, "Confirmed by Amina")
        self.assertEqual(CheckIn.objects.get().state, "acknowledged")

    def test_link_get_alone_does_not_acknowledge(self):
        n = self.open_checkin()
        self.client.get(f"/ack/{n.ack_token}/")
        self.assertEqual(CheckIn.objects.get().state, "open")   # link scanners/previews must not confirm

    def test_link_help(self):
        n = self.open_checkin()
        with override_settings(EMAIL_BACKEND=MEM):
            r = self.client.post(f"/ack/{n.ack_token}/", {"action": "needs_help"})
        self.assertContains(r, "Help requested")

    def test_bad_token(self):
        self.assertEqual(self.client.get("/ack/00000000-0000-0000-0000-000000000000/").status_code, 404)


class PrivacyExportDeleteTests(Base):
    def setUp(self):
        super().setUp()
        seed_history(self.h, days=3)

    def test_privacy_page_public_and_generated_from_code(self):
        r = self.client.get("/privacy/")
        self.assertContains(r, "never request, receive, store or display video")
        self.assertContains(r, "snapshot_url")
        self.assertContains(r, "event_type")

    def test_export_contains_data_no_secrets(self):
        self.login()
        r = self.client.get(f"/h/{self.h.pk}/export/")
        self.assertIn("attachment", r["Content-Disposition"])
        d = json.loads(r.content)
        self.assertEqual(d["household"]["name"], "Mama")
        self.assertEqual(len(d["events"]), 6)
        self.assertEqual(len(d["members"]), 2)
        text = r.content.decode()
        self.assertNotIn("password", text)
        self.assertNotIn(PW, text)

    def test_delete_requires_exact_name_and_cascades(self):
        self.login()
        r = self.client.post(f"/h/{self.h.pk}/delete/", {"confirm": "wrong"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Household.objects.exists())
        CheckIn.objects.create(household=self.h, date=today())
        self.client.post(f"/h/{self.h.pk}/delete/", {"confirm": "Mama"})
        self.assertFalse(Household.objects.exists())
        for model in (Event, CheckIn, Membership, EmergencyContact):
            self.assertEqual(model.objects.count(), 0, model)
        self.assertTrue(User.objects.filter(username="amina").exists())   # accounts remain

    def test_settings_retention_bounds(self):
        self.login()
        base = {"window_start": "05:00", "check_by": "09:00", "escalation_minutes": 20, "sensor_silent_hours": 36}
        r = self.client.post(f"/h/{self.h.pk}/settings/", {**base, "retention_days": 3})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/h/{self.h.pk}/settings/", {**base, "retention_days": 30, "latitude": "6.5", "longitude": "3.4",
                                                           "daylight_aware": "on"})
        self.assertEqual(r.status_code, 302)
        self.h.refresh_from_db()
        self.assertEqual((self.h.retention_days, self.h.daylight_aware), (30, True))
        r = self.client.post(f"/h/{self.h.pk}/settings/", {**base, "retention_days": 30, "latitude": "95"})
        self.assertEqual(r.status_code, 200)
