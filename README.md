# Quiet Morning

Amazon Developer Hackathon 2026, **Ring track**. A gentle morning check-in for the people who care for
someone living alone. It learns the usual rhythm of a Ring home from door/motion **metadata only**, and if a
morning goes unexpectedly quiet it tells the right person first, then the next, and never sees video.

> Alerts fire on the **absence** of expected activity, always with a reason ("Usually first activity around
> 06:35 on Saturdays; today nothing by 08:10") and a confidence score.

## Quick start (no Ring, Redis, Twilio or internet needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py simulate_demo --reset      # scripted scenarios, prints an audit trail
.venv/bin/python manage.py runserver                  # http://localhost:8000  (log in: amina / demo-pass-12345)
.venv/bin/python manage.py test                       # 113 tests
```

Docker (web + celery worker + celery beat + redis): `cp .env.example .env && docker compose up --build`.

## What is in it

**Accounts and team**: Django auth (login, signup), several caregivers per household, roles (owner, caregiver,
viewer), 7-day single-use invite links by email, escalation order per caregiver, per-caregiver channel
preferences and quiet hours, emergency contacts who need no account. Non-members get a 404 (no leaking that a
household exists).

**Smarter signal** (`watch/rules.py`, every decision returns an explainable `Assessment`)
- Per-weekday baselines (falls back to all days when a weekday has < 3 samples); 8 weeks of history.
- Optional sunrise awareness (NOAA formula from an approximate lat/lon; half the sunrise change, capped at 30 min).
- Away/holiday mode, snooze, and "expect them late today" notes that move the deadline and appear in the explanation.
- Sensor health separates *device* problems (offline flag, low battery, no events of any kind for N hours) from a
  quiet morning. A device problem sends a **device notice** to the owner and first caregiver, not the emergency chain.
- Confidence score for both cases. Drift detection compares the last 2 weeks with the previous 6 and shows a gentle
  early warning (never an alert), with a weekly sparkline.

**Escalation** (`watch/escalation.py`, `watch/notify.py`): gentle nudge to the person (opt-in), caregiver 1,
caregiver 2, ..., emergency contacts, with a configurable wait between steps. Acknowledge from the dashboard or a
one-tap link in the message (GET never confirms, so link previews cannot silently acknowledge). "Needs help" alerts
the rest of the chain immediately. When someone confirms, the others already contacted get a follow-up so nobody
double-calls. Quiet hours hold back SMS/WhatsApp/push (email still lands) until the 3rd step or a help request. Failed
sends retry with backoff (1, 5, 15 min, 4 attempts) and exceptions in one channel never block another. Every
attempt is a `Notification` row, shown on the History page.

**Channels** (`watch/channels.py`): email (SMTP), SMS and WhatsApp via Twilio, Web Push (VAPID via `pywebpush`). Twilio
and push are config-flagged and are logged stubs without keys. Missing `requests` dependency fixed.

**UX**: mobile-first, installable PWA (manifest, service worker, icons), dark mode, state hero with glyph + text
(never colour alone), day strip, 14-day ribbon, timeline that merges Ring events with escalation steps, large-type
high-contrast mode, empty states, onboarding wizard (who, morning, team, trust), weekly summary email
(Mondays 08:00) with an in-app preview.

**Privacy and trust** (`watch/privacy.py`): event payloads pass an allowlist before storage, and again in
`Event.save()`; media fields, URLs and long strings are dropped (tested). The public `/privacy/` page is generated from
the same constants. Owners confirm consent during setup, choose a retention window (14-365 days, purged daily), can
download everything as JSON and delete a household (typed confirmation).

**Ops**: `/healthz/` (add `?deep=1` to ping the broker), JSON structured logs, all config from env
(`.env.example`), Dockerfile + compose, webhook that answers malformed input with 400 (never 500), dedupes by event id,
rejects stale/future timestamps. See `docs/deploy-aws.md`.

## Demo scenarios

`simulate_demo --scenario normal|late|offline|quiet|all` on a fake clock:
1. **normal**: motion at the usual time; nobody bothered.
2. **late**: a late-but-normal morning, then a hospital day marked "expect late"; no alerts.
3. **offline**: three silent days, 4% battery: a *device* notice to two people, no emergency chain.
4. **quiet**: nudge to Mama, Amina by email only (her phone is silent), Tunde by SMS/WhatsApp, Tunde confirms, chain stops.

The database keeps the last scenario's state so the dashboard is ready to film. Script: `docs/video-script.md`.

## Verified vs unverified

Verified here (automated tests, 113): rules engine, escalation, quiet hours, retry/backoff, auth and roles, invites,
onboarding, privacy enforcement, export/delete, retention, webhook robustness, weekly summary, demo scenarios, PWA
manifest/service-worker responses, push subscription endpoint. Also verified by hand: `docker compose up` builds and
runs web + worker + beat + redis, `/healthz/?deep=1` reports the broker, and the worker executes `tick`.

**Not verified (needs real accounts, devices or Ring access):**
- **Ring webhook signature scheme is a placeholder (`watch/webhook.py`, marked TODO-verify).** So are the payload
  field names (`event_type`, `device_id`, `location_id`, `created_at`, `event_id`). We do not know that Ring sends
  heartbeat, battery or offline events at all; the sensor-health logic is written against our own generic schema. Do
  not expose the webhook to real traffic until this is replaced.
- Whether motion/door metadata reaches third parties when Ring video E2EE is on.
- Real Twilio delivery (calls are unit-tested against a mock; WhatsApp business-initiated messages need approved templates).
- Web Push in real browsers: the server path is tested with a mocked `pywebpush`; browser subscription was not run
  in a real browser. It needs HTTPS (localhost excepted), and iOS only supports it for installed home-screen apps (our understanding, unverified).
- AWS deployment (see `docs/deploy-aws.md`), SES/SNS delivery, PWA install prompts on real phones, screen-reader testing.

## Honest gaps
- SQLite by default; no multi-instance safe locking of escalation steps (run one beat + one worker, or move to Postgres and add row locks).
- Timezone is one global setting, not per household. Sunrise uses that zone's offset.
- No SMS/WhatsApp reply handling, no password reset email flow (Django's views are not wired), no rate limiting on login.
- Not a medical or emergency service. Silence is ambiguous (person out, guests, device asleep); wording reflects that.
- The person being cared for has no login; consent is a checkbox by the owner, not a verified flow.

Docs: `docs/video-script.md`, `docs/amazon-feedback.md`, `docs/deploy-aws.md`. License: MIT
