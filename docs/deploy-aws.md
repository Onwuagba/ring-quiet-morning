# Deploying on AWS

**Status: nothing here was executed against AWS** (no credentials were available). Verified locally: `docker compose up --build`
runs web, worker, beat and redis; 170 unit tests pass with AWS calls faked; `infra/quiet-morning.yml` passes `cfn-lint`.
Everything else is a plan, marked (unverified). Service decisions and sources: [AWS_STACK.md](AWS_STACK.md).

## Two supported shapes
1. **Compose / simple** (unchanged): web + Celery worker + Celery beat + Redis, SQLite or Postgres.
2. **AWS-native events** (this doc): the same Django app, but Celery/Redis are replaced by SQS + EventBridge Scheduler.

```
Ring cloud --HTTPS--> Lambda function URL (HMAC check) --> SQS --+
EventBridge Scheduler (tick 1 min, purge, weekly) -------------> SQS --> ECS Fargate worker: manage.py sqs_worker
                                                              (DLQ + alarms)         |  --> Bedrock Converse (optional)
Browsers/Telegram --> ALB --> ECS Fargate web (gunicorn) --> RDS Postgres <-----------+  --> SES SMTP, Telegram API
```

## Steps (unverified unless noted)
1. **Image**: build the `Dockerfile` with `--build-arg WITH_AWS=1` (adds boto3, psycopg, strands-agents from `requirements-aws.txt`), push to ECR. One image, two ECS services: web (default CMD) and worker (`python manage.py sqs_worker`).
2. **Ingest package**: `cd infra && zip ingest.zip lambda_ingest.py && aws s3 cp ingest.zip s3://<bucket>/ingest.zip`.
3. **Stack**: `aws cloudformation deploy --template-file infra/quiet-morning.yml --stack-name quiet-morning --capabilities CAPABILITY_IAM --parameter-overrides LambdaCodeBucket=<bucket> RingWebhookSecret=<secret> DbSubnetIds=<s1,s2> DbSecurityGroupId=<sg> BedrockModelArns=<arn,...>`. Creates SQS + DLQ, Lambda + function URL, Scheduler schedules, Secrets Manager secret, RDS Postgres (RDS-managed master password in Secrets Manager), alarms, and `AppTaskRole` (SQS receive/delete only + Bedrock on the ARNs you list). Lint check (done): `cfn-lint infra/quiet-morning.yml`.
4. **Wire the app** (ECS task env; secrets via the task *execution* role from Secrets Manager, not baked in): `DATABASE_URL` (endpoint from stack output; password from the RDS-managed secret; build the URL in your task definition), `SQS_QUEUE_URL`, `AWS_REGION`, `DJANGO_SECRET_KEY`, `RING_WEBHOOK_SECRET` (same value as the stack secret), Telegram vars, `SITE_URL` etc. Run `python manage.py migrate` as a one-off task. Do **not** run Celery beat in this shape (Scheduler replaces it).
5. **Bedrock (optional)**: in the Bedrock console enable access to your chosen model (TODO-verify how access is granted for your account), create a Guardrail (denied topics: medical advice/diagnosis; word filters; PII), publish a version, then set `BEDROCK_MODEL_ID`, `BEDROCK_WORDING=1`, `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION`. Advisor: `AI_ADVISOR=bedrock` (or `strands`). Check `AiUsage` rows and CloudWatch after a day; lower `BEDROCK_DAILY_CALL_CAP` if you want a tighter bound.
6. **HTTPS and hosts**: ALB + ACM for the web service. Set `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS=https://your.domain`, `SITE_URL`, `SECURE_COOKIES=1`. Health check path `/healthz/` (the `?deep=1` variant pings Redis and is for the Celery shape only).
7. **Email**: SES SMTP: `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, verified `DEFAULT_FROM_EMAIL`. New SES accounts start in a sandbox (verified recipients only).
8. **Telegram**: as before: BotFather token, `TELEGRAM_WEBHOOK_SECRET`, then `python manage.py telegram_bot --set-webhook https://your.domain` once. Needs egress (NAT) to api.telegram.org.
9. **Ring webhook**: register the stack output `RingWebhookUrl` as the Webhook URL in the Ring developer portal (Account linking page; the endpoint must answer HTTP 200/2xx within 5 seconds) and store Ring's HMAC signing key in the secret the Lambda reads (`X-Signature: sha256=<hex>` over the raw body is verified in `infra/lambda_ingest.py` and again in `watch/webhook.py`).
10. **Locking**: with Postgres, `escalation.advance/acknowledge` take `SELECT ... FOR UPDATE` on the check-in row, so several workers are safe. With SQLite (Compose default) keep one worker. Multi-worker behaviour on Postgres is unit-tested only for stale copies (SQLite cannot exercise true concurrency): TODO-verify with two real workers.
11. **Alarms to subscribe an SNS topic/email to**: `DlqAlarm`, `QueueAgeAlarm` (stale tick = late escalations), `IngestErrorAlarm`. Logs are JSON on stdout (awslogs driver).

## Risk and cost notes
- The safety-critical path (rules -> escalation -> Telegram/SES) has no AWS AI dependency. Bedrock, Guardrails, the advisor and Strands can all be down or disabled and every message still goes out with the deterministic template.
- A dead tick means no quiet-morning detection: hence the queue-age alarm. Consider a second, independent liveness check (e.g. a CloudWatch synthetic on `/healthz/`).
- Function URL with `AuthType: NONE` is publicly callable; bad signatures are dropped at the edge, reserved concurrency (default 10, RPS about 10x) bounds abuse.
- Cost floor is RDS + Fargate; scheduler/SQS scale with the 1-minute tick (43,200/month). Bedrock is capped per day. No prices were verified; see AWS_STACK.md "Cost drivers".
- If you host on AWS for the AWS Builder challenge, check the challenge rules for what counts as hosting (TODO-verify).
