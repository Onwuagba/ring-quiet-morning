# Demo video script (about 2:30)

Setup: `python manage.py simulate_demo --reset --scenario quiet` (prints simulated Telegram messages unless `TELEGRAM_BOT_TOKEN` is set; for a live shot, use a real bot and link your own chat first), `runserver`, log in as `amina` on a phone-width window.
Show the terminal output once for the scenario story, then the UI.

| Time | On screen | Voice-over |
|---|---|---|
| 0:00 | Landing page | "Millions of us worry about a parent living alone. Cameras are invasive. Quiet Morning uses only door and motion metadata from Ring, and never sees video." |
| 0:15 | Terminal: scenario 1 and 2 output | "It learns their routine per weekday. A normal morning: nobody is bothered. A late morning, or a day Amina marked 'hospital appointment': still nobody bothered." |
| 0:35 | Dashboard, green hero, "usually 06:35, motion at 06:40" | "Every state says why, in plain words." |
| 0:50 | Terminal: scenario 3 (offline) | "Three silent days and 4% battery. That is a device problem, not a quiet morning, so we send a device notice, not an alarm." |
| 1:10 | Dashboard in quiet state: amber hero, confidence pill | "A real quiet morning. 'Usually 07:30 on Saturdays; today nothing by 08:10.' Confidence 76%." |
| 1:30 | Terminal audit trail | "First a gentle hello to Mama. Then Amina, by email only because her phone is silent while she sleeps. Then Tunde on Telegram, with All OK and Need help buttons." |
| 1:55 | Tap All OK in Telegram (or the ack link); dashboard turns green | "Tunde confirms with one tap in Telegram, and the message updates to show it. The chain stops, and Amina is told so nobody double-calls." |
| 2:10 | History page, then Privacy page, Settings > export/delete | "Every message is logged. The privacy promises are enforced in code and tested. Owners can export or delete everything." |
| 2:25 | Large-type toggle, install prompt | "Large type for older caregivers, installable on any phone." |
| 2:30 | Title card | "Quiet Morning. Peace of mind without watching." |

Do not claim: that Ring delivers heartbeat/battery events, that the webhook signature matches Ring's, or that push and
Telegram were tested live. Say "prototype on simulated Ring events".
