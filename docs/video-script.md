# Demo video script (about 2 min)

Setup: `python manage.py migrate && python manage.py simulate_demo --reset`, then `runserver` and open the printed `/h/<token>/` dashboard.

1. (0:00) Hook, voice-over: "Most safety tech watches for emergencies. But the worst mornings are silent ones: nothing happens."
2. (0:15) Terminal: run `simulate_demo`. Point at step 1: two weeks of door/motion metadata, learned usual morning, adaptive check-in time (earlier than the fixed 09:00).
3. (0:40) Steps 2-3: five minutes before the deadline, no alert. Deadline passes, one gentle check-in goes out. Show the email/console message: calm wording, not an alarm.
4. (1:05) Browser: dashboard. Show today's timeline (empty), the "check-in sent" card, usual-morning panel, quiet-window settings.
5. (1:30) Tap All OK. Card flips to confirmed.
6. (1:45) Privacy close: "Metadata only. No video, no faces. We alert on absence, never on activity." Mention Ring's E2EE limits honestly (see README) and that the Ring integration is sandbox-pending.

Note: the demo uses simulated events, not a live Ring device. Say so on screen.
