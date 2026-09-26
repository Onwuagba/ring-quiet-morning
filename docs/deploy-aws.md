# Deploying on AWS

**Status: nothing here was executed against AWS.** What *was* verified locally: `docker compose up --build` runs web, worker,
beat and redis, `/healthz/?deep=1` returns ok, and the worker runs the `tick` task. Everything AWS-specific below is a plan
to confirm, marked (unverified).

## Shape
```
Ring cloud --HTTPS--> ALB --> ECS Fargate: web (gunicorn)      --> RDS Postgres (recommended; unverified)
                                             worker (celery)    --> ElastiCache Redis (broker)
                                             beat (celery beat; exactly ONE task)
email: SES SMTP  |  chat: Telegram Bot API (webhook back to the ALB)  |  push: VAPID (no AWS service needed)
```

## Steps (unverified)
1. **Image**: build the `Dockerfile`, push to ECR. One image, three ECS services differing only by command
   (`gunicorn ...` default, `celery -A quietmorning worker -l info`, `celery -A quietmorning beat -l info`).
2. **Database**: SQLite is fine for a demo (mount EFS at `/data`, unverified for locking). For real use switch `DATABASES` to
   Postgres (add `psycopg` and read `DATABASE_URL`; not implemented yet). Run only one beat and, until Postgres row locks are added, one worker.
3. **Redis**: ElastiCache; set `REDIS_URL`.
4. **Secrets**: put `DJANGO_SECRET_KEY`, `RING_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `VAPID_PRIVATE_KEY`, SMTP credentials in Secrets Manager / SSM and inject as ECS env. Never bake them into the image.
5. **HTTPS and hosts**: terminate TLS at the ALB with ACM. Set `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS=https://your.domain`, `SITE_URL=https://your.domain`, `SECURE_COOKIES=1`. The app trusts `X-Forwarded-Proto`. Enable `SECURE_HSTS_SECONDS` only after HTTPS is confirmed.
6. **Health check**: ALB target group path `/healthz/` over HTTP (leave `SECURE_SSL_REDIRECT=0`, or health-check over HTTPS). Use `/healthz/?deep=1` for an alarm that also checks Redis.
7. **Email**: SES SMTP: set `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and a verified `DEFAULT_FROM_EMAIL`. New SES accounts start in a sandbox that only sends to verified addresses.
8. **Telegram**: create the bot with @BotFather, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, then run once `python manage.py telegram_bot --set-webhook https://your.domain` (one-off ECS task). Telegram needs a public HTTPS endpoint on port 443/80/88/8443 with a valid certificate (ACM on the ALB is fine); route `/webhooks/telegram/` to the web service. The webhook is authenticated only by the secret header, so keep it in Secrets Manager. Outbound calls to api.telegram.org need NAT/egress. Unverified against a live bot.
9. **Push**: generate keys with `vapid --gen` (py-vapid is installed with pywebpush), set `VAPID_PUBLIC_KEY` (base64url application server key) and `VAPID_PRIVATE_KEY`. Needs HTTPS.
10. **Ring webhook**: point Ring at `https://your.domain/webhooks/ring/` only after replacing the placeholder signature check in `watch/webhook.py` with Ring's documented scheme.
11. **Logs**: JSON lines on stdout go to CloudWatch via the awslogs driver. Useful filters: `reason` (webhook rejects), `escalation exhausted`, `notification failed permanently`.
12. **Data**: `purge_old_data` runs daily via beat (retention per household). Back up the DB; exported JSON is per household from Settings.

## Cost / scale note
A single small Fargate task per service plus a small Redis is enough for hundreds of households; the tick task is O(households) every minute (unverified beyond the demo).
