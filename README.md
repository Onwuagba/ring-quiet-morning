# Ring Quiet Morning

Amazon Developer Hackathon 2026 — **Ring track**. A check-in for people who care for someone
living alone. Ring event metadata (motion, ding, door) builds the usual morning; if **nothing**
has happened by a set time, Quiet Morning sends a gentle check-in to the caregiver. It alerts
on the absence of activity, never on activity, and it never stores video.

## Run

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py test
python manage.py runserver                       # POST /webhooks/ring/
celery -A quietmorning worker -B -l info         # checks every 5 min (needs Redis)
```

## Demo and UI

```bash
python manage.py simulate_demo --reset   # 14 learned mornings, quiet day, check-in, caregiver confirms
python manage.py runserver               # caregiver dashboard at /h/<access_token>/ (token printed above, also in admin)
```

Dashboard: today's timeline, learned usual morning, quiet-window settings, All OK / Need help buttons.
The dashboard URL contains a secret token per household; treat it like a password (no login system yet).

## How it works

- **Rule:** alert only when there is no activity between `window_start` and the deadline, once per day.
- **Adaptive baseline:** with 5+ mornings of history, deadline = 90th-percentile first activity + 30 min, kept within window_start+60min and check_by+120min. Otherwise the fixed `check_by` applies. Toggle per household (`adaptive`).
- **Notifications:** `NOTIFY_CHANNELS` (default `console,email`). Set `WHATSAPP_ENABLED=1` plus `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` to send; without keys it is a logged stub. Email uses the console backend unless `EMAIL_BACKEND` is set. Set `SITE_URL` for correct links.
- **Webhook hardening:** dedupe by event id (or body hash), reject events older than `RING_REPLAY_WINDOW_SECONDS` (default 900), strip video/snapshot URLs.

## Ring E2EE limits and honest caveats

- Ring's end-to-end encryption protects video; we do not know which event metadata (motion, door) is still delivered to third parties when it is on. Unverified; confirm in the sandbox.
- **The signature scheme in `watch/views.py` is a placeholder (TODO-verify).** The real Ring header and algorithm are not known here and were not invented beyond a generic HMAC stand-in. Until replaced, do not expose the webhook to production traffic.
- Silence is ambiguous: an offline device or a person who is out looks the same as a quiet home. Wording is a check-in, not an emergency alert. This is not a medical or safety-certified system.
- Timestamp replay protection relies on the payload's `created_at`, not a signed timestamp.

## Next steps

1. Create a Ring developer account and run `ring-api-helloworld` against the sandbox.
2. Confirm event fields, ids and the real signature header, then update `watch/views.py`.
3. Add caregiver login instead of the URL token; WhatsApp reply links.

Docs: `docs/video-script.md`, `docs/amazon-feedback.md`. License: MIT
