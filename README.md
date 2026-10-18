# Quiet Morning

Amazon Developer Hackathon 2026, **Ring track**. A gentle morning check-in for the people who care for
someone living alone. It learns the usual rhythm of a Ring home from door/motion **metadata only**, and if a
morning goes unexpectedly quiet it tells the right person first, then the next, and never sees video.

> Alerts fire on the **absence** of expected activity, always with a reason ("Usually first activity around
> 06:35 on Saturdays; today nothing by 08:10") and a confidence score.

## Quick start (no Ring, Redis, Telegram bot or internet needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py simulate_demo --reset      # scripted scenarios, prints an audit trail
.venv/bin/python manage.py runserver                  # http://localhost:8000  (log in: amina / demo-pass-12345)
.venv/bin/python manage.py test                       # 170 tests
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
double-calls. Quiet hours hold back Telegram/push (email still lands) until the 3rd step or a help request. Failed
sends retry with backoff (1, 5, 15 min, 4 attempts) and exceptions in one channel never block another. Every
attempt is a `Notification` row, shown on the History page.

**Channels** (`watch/channels.py`, `watch/telegram.py`): email (SMTP), Telegram (Bot API), Web Push (VAPID via
`pywebpush`). Telegram and push are logged stubs without keys. Telegram messages carry **All OK** / **Need help**
inline buttons that act exactly like the dashboard and the ack link, answer the button press, edit the message to
show the result, and trigger the usual "someone has it" follow-up to the other caregivers. Quiet hours, retry/backoff,
audit rows and per-caregiver preferences apply to Telegram like every other channel. Chat ids are never logged, shown,
exported or written to audit rows, and the bot token is never stored or logged (errors are scrubbed of it).

### Telegram setup
1. In Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`. It replies with a
   token (looks like `123456:ABC...`). Treat it as a password.
2. Set env vars (`.env`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` (no @), and `TELEGRAM_WEBHOOK_SECRET`
   (random, `A-Z a-z 0-9 _ -`, 1-256 chars).
3. Receive updates one of two ways:
   - **Local dev, no HTTPS**: `python manage.py telegram_bot --poll` (long-polls `getUpdates`; removes any webhook).
   - **Production (HTTPS)**: `python manage.py telegram_bot --set-webhook https://your.domain` registers
     `https://your.domain/webhooks/telegram/` with the secret. `--info` shows status, `--delete-webhook` removes it.
4. Each person connects in the app: caregivers use **My alerts, Connect Telegram**; owners create links for emergency
   contacts (Team) and, optionally, for the person being cared for (Settings). The link is `t.me/<bot>?start=<one-time
   token>` (24 h, single use); pressing **Start** in Telegram binds the chat. `/stop` in the chat disconnects.

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
4. **quiet**: nudge to Mama, Amina by email only (her phone is silent), Tunde on Telegram, Tunde taps All OK, chain stops.

The database keeps the last scenario's state so the dashboard is ready to film. Script: `docs/video-script.md`.

## Verified vs unverified

Verified here (automated tests, 170): rules engine, escalation, quiet hours, retry/backoff, auth and roles, invites,
onboarding, privacy enforcement, export/delete, retention, webhook robustness, weekly summary, demo scenarios, PWA
manifest/service-worker responses, push subscription endpoint. Also verified by hand: `docker compose up` builds and
runs web + worker + beat + redis, `/healthz/?deep=1` reports the broker, and the worker executes `tick`.

**Not verified (needs real accounts, devices or Ring access):**
- **Ring integration is built from the Ring developer docs but was not run against Ring.** The webhook receiver
  follows the documented format (`X-Signature: sha256=<hex HMAC-SHA256 of the raw body>`, the `meta`/`data` envelope,
  events `motion_detected`, `button_press`, `device_online`, `device_offline`, matched on `meta.account_id`; see
  `watch/ringapi.py`). Not yet confirmed against a real Ring account: the exact signed bytes with live traffic, the
  Ring sandbox host, and OAuth account linking (a token-refresh helper exists, the consent flow does not). Ring sends no
  battery webhook, so battery comes from polling `GET /v1/devices/{id}/status` (`poll_ring_status`, needs
  `RING_ACCESS_TOKEN`). Run `RING_SIMULATOR=1 python manage.py runserver` and open `/simulator/` to drive the receiver
  with signed Ring-format events and a simulated clock.
- Whether motion/door metadata reaches third parties when Ring video E2EE is on.
- **Telegram against the live Bot API.** No bot token was available: HTTP is unit-tested against mocks and a fake
  transport, and the request shapes follow the Bot API docs as we understood them (the doc fetch tool truncated the page;
  the 4096-character text limit, 64-byte `callback_data` limit and HTML escaping of `& < >` are from memory and should
  be confirmed at core.telegram.org/bots/api). The webhook secret header, deep-link `/start` payload and button flow have
  never run against Telegram's servers.
- Web Push in real browsers: the server path is tested with a mocked `pywebpush`; browser subscription was not run
  in a real browser. It needs HTTPS (localhost excepted), and iOS only supports it for installed home-screen apps (our understanding, unverified).
- AWS deployment (see `docs/deploy-aws.md`), SES delivery, PWA install prompts on real phones, screen-reader testing.

## Honest gaps
- SQLite by default; no multi-instance safe locking of escalation steps (run one beat + one worker, or move to Postgres and add row locks).
- Timezone is one global setting, not per household. Sunrise uses that zone's offset.
- Telegram: no free-text replies (only the two buttons, `/start`, `/stop`); a person who blocks the bot is a failed send that retries then gives up; the person being cared for needs a Telegram account to get the gentle hello. No password reset email flow (Django's views are not wired), no rate limiting on login.
- Not a medical or emergency service. Silence is ambiguous (person out, guests, device asleep); wording reflects that.
- The person being cared for has no login; consent is a checkbox by the owner, not a verified flow.

Docs: `docs/AWS_STACK.md`, `docs/video-script.md`, `docs/amazon-feedback.md`, `docs/deploy-aws.md`. License: MIT

## AWS (optional, all off by default)

The deterministic product needs none of this. Each piece is switched on by env vars and is tested offline with fakes;
**none of it has run against a live AWS account.** Full decisions with sources: `docs/AWS_STACK.md`.

- **Bedrock Converse** (`BEDROCK_MODEL_ID`, `BEDROCK_WORDING=1`): a warm opener on the soft nudge / caregiver messages and a short
  weekly narrative. The model never writes the safety text ("not an emergency", the ack link, emergency-contact, help and device
  messages stay templates). Output is validated (no medical terms, links, invented numbers); any doubt falls back to the template.
  Optional Guardrail, hard daily call cap.
- **Care-coordinator advisor** (`AI_ADVISOR=bedrock|strands`): reviews only ambiguous mornings and suggests nudge/wait/escalate.
  Advisory: it cannot suppress or delay an escalation (tests compare full timelines); `escalate` only skips the nudge.
- **Postgres** (`DATABASE_URL`) with `SELECT ... FOR UPDATE` on check-ins so several workers are safe.
- **AWS-native events**: `infra/quiet-morning.yml` (Lambda function URL -> SQS + DLQ, EventBridge Scheduler instead of Celery beat,
  RDS Postgres, Secrets Manager, alarms, least-privilege task role), worker `python manage.py sqs_worker`. See `docs/deploy-aws.md`.
- Deliberately **not** used: AgentCore, SageMaker (no honest fit; reasons in AWS_STACK.md), SNS, DynamoDB, API Gateway.
