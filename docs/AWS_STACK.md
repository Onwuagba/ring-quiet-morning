# AWS stack: what earned a place, what did not, and what is verified

Status legend. **DOCS** = read in the official AWS/Strands page cited (fetched 2026-09-26). **CFN-LINT** = passes
`cfn-lint` schema validation offline. **OFFLINE-TEST** = exercised by unit tests with fakes. **UNVERIFIED-LIVE** =
never run against AWS (no credentials were available). **TODO-verify** = not confirmed even in docs.
Nothing in this file was verified against a live AWS account.

## Decisions

| Service | Decision | Why (product reasons first) |
|---|---|---|
| Amazon Bedrock Converse | **Integrated, optional** (`watch/aws/bedrock.py`, `wording.py`) | A model is good at *tone*: a warm opener for a nudge/caregiver message and a calm weekly narrative. It is bad at judgment we must be able to explain, so it never decides anything and never writes the safety-critical text. |
| Bedrock Guardrails | **Integrated, optional** (`BEDROCK_GUARDRAIL_ID/VERSION`) | Defence in depth on top of our own output validator. Passed via `guardrailConfig` on Converse. An intervention is treated as "use the template". |
| Strands Agents SDK | **Integrated, optional** (`AI_ADVISOR=strands`, `watch/aws/advisor.py`) | A "care coordinator" reviews only *ambiguous* mornings (expected-late note, low confidence, degraded sensor, sparse history) and suggests nudge/wait/escalate with reasons. Advisory only; see safety floor below. Strands is the tool-using variant; `AI_ADVISOR=bedrock` is the same contract via plain Converse. |
| Bedrock AgentCore | **Not integrated** | See below. Documented as a future hosting target for the advisor, not shipped as dead code. |
| SageMaker | **Not integrated** | No real model fits yet. See below. |
| SQS + dead-letter queue | **Integrated** (`watch/aws/queue.py`, `manage.py sqs_worker`, `infra/quiet-morning.yml`) | Decouples Ring webhook bursts from the worker, gives retries + DLQ + an alarm, replaces Redis for the AWS path. |
| Lambda function URL (Ring edge) | **Integrated** (`infra/lambda_ingest.py`) | Cheapest public HTTPS endpoint, no API Gateway needed. `X-Signature` HMAC check in code, reserved concurrency as a throttle. |
| EventBridge Scheduler | **Integrated as an option** | Replaces Celery beat (a single point of failure that must run exactly once). Celery path still works unchanged. |
| RDS Postgres | **Integrated** (`DATABASE_URL`, template) | Real row locks for escalation (`select_for_update`), backups, multi-worker safety. |
| Secrets Manager | **Integrated** (Ring secret in template; RDS-managed master password) | No secrets in images/env files. |
| CloudWatch | **Integrated** (3 alarms; JSON logs already existed) | DLQ depth, oldest-message age (stale tick means late escalations), ingest errors. |
| SES | **Kept via SMTP** (already there) | Django SMTP backend. No SES API code: it would add nothing. |
| SNS | **Not used** | SQS + our own channel fan-out already covers it; SNS SMS/push would duplicate Telegram/web push and add per-message cost. |
| DynamoDB | **Not used** | Relational data (households, roles, audit trail) with constraints; Postgres is the better fit. |
| API Gateway | **Not used** | Function URL is enough for one POST endpoint; API Gateway adds cost/complexity we do not need. (Would be the choice if we wanted usage plans, WAF-managed rules, or a custom domain without CloudFront.) |
| Kiro | Excluded by the task. | |

### Safety floor for the advisor (enforced in code, tested)
1. The rule engine (`watch/rules.py`) decides that a check-in exists; the advisor is called *after*, never for device notices, and only when the picture is ambiguous.
2. `wait` is recorded and ignored. The chain and its timings are byte-for-byte the same (test compares full timelines).
3. `escalate` may only skip the soft nudge to the monitored person and message caregivers immediately.
4. Garbage, injection text in notes, unknown actions, exceptions, timeouts, missing packages: no advice, chain unchanged.
5. Advisor reasons pass the same banned-wording filter (no medical terms, no links, no alarmist words) before being stored.
6. Inputs are first name/label, times, confidence, device status and a note truncated to 140 chars inside `<note>` tags marked untrusted. No contact details, no event payloads.

### Why not AgentCore
Facts (DOCS): AgentCore Runtime is a serverless host where each user session runs in a dedicated microVM (up to 8 h per session, or 14 days on Instances), built for interactive and long-running agents, with Memory (short/long-term), Gateway (turn APIs/Lambda into MCP tools), Identity, Observability, Policy etc. as separate modular services. Our advisor is a stateless, single-shot review of one morning; we deliberately store no conversation history (privacy promise), so Memory has no job; the "tools" are in-process read-only facts, so Gateway has no job; Identity is Django. Deploying to AgentCore Runtime would add a network hop and a new failure mode on the safety path for no product gain today. What would change our mind: a multi-turn caregiver assistant ("why did you alert?") that needs isolated sessions, or hosting the advisor outside the Django VPC. Then the `advisor.advise()` contract is the seam. (Entry-point shape from the AgentCore docs/search: `from bedrock_agentcore.runtime import BedrockAgentCoreApp` with `@app.entrypoint`: TODO-verify; not used.)

### Why not SageMaker
Facts (DOCS): SageMaker built-in Random Cut Forest is an unsupervised anomaly-score algorithm; training reads from S3 (CSV/RecordIO-protobuf), inference on an endpoint (docs recommend `ml.c5` family). Our signal is a *one-dimensional* series of at most ~56 daily first-activity times per household. A robust median/spread baseline already yields a plain-language reason ("usually 07:30 on Saturdays"), which is the product's core promise and a safety property; an RCF score is an opaque number that we would have to explain after the fact, needs a trained model per household or a pooled one (privacy question), and an always-on endpoint costs more than the rest of the stack for a handful of households. Revisit when there is a fleet with enough pooled, consented data to learn cross-household patterns (multi-dimensional: device mix, weekday, season), using batch transform/serverless inference, with the rule engine kept as the floor. No fake model was added.

## Verified facts with sources (DOCS)

Bedrock Converse
- Converse/ConverseStream are on the `bedrock-runtime` endpoint; request has required `modelId`, plus `messages`, `system`, `inferenceConfig`, `guardrailConfig`; response is normalized to `output`, `stopReason`, `usage`, `metrics`. https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html (the older `conversation-inference-call.html` now just says "moved").
- boto3 shape: `client.converse(modelId=..., messages=[{"role":"user","content":[{"text":...}]}], system=[{"text":...}], inferenceConfig={...})`; `response['stopReason']`, `response['output']['message']['content'][i]['text']`. Same page and https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html
- Guardrail: `guardrailConfig={"guardrailIdentifier","guardrailVersion","trace":"enabled"}`; when it blocks, `stopReason == "guardrail_intervened"` and `output` holds the configured blocked message. If any `guardContent` block is present, only those blocks are evaluated. Tool inputs/results are not evaluated by `guardrailConfig`. https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html
- `ApplyGuardrail` (standalone): `source` INPUT|OUTPUT, `content:[{text:{text}}]`, response `action` GUARDRAIL_INTERVENED|NONE. https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html . We do not use it (Converse `guardrailConfig` covers our path).
- Model IDs (model cards, https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.md and https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.md):
  - Nova Lite: in-region `amazon.nova-lite-v1:0`; geo profiles `us.amazon.nova-lite-v1:0`, `eu.amazon.nova-lite-v1:0`; global not supported; Converse yes; Guardrails yes; structured outputs no.
  - Claude Haiku 4.5: `bedrock-runtime` requires an inference profile (`us.`/`eu.`/`au.`/`jp.anthropic.claude-haiku-4-5-20251001-v1:0` or `global.anthropic.claude-haiku-4-5-20251001-v1:0`); bare ID is only for the `bedrock-mantle` endpoint. Marked "EOL no sooner than Oct 16, 2026".
  - Model list of the day (https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.md) includes Nova (Micro/Lite/Pro/Premier, Nova 2 Lite) and Claude 4.x/5.x families. We ship **no default** `BEDROCK_MODEL_ID`; the operator chooses one their account can call. `.env.example` suggests the Nova Lite ID only as a documented example.
  - Model access/enablement and account quotas were not checked: TODO-verify in console.
- Pricing: TODO-verify at https://aws.amazon.com/bedrock/pricing/ (not fetched; no numbers are quoted here).

Strands Agents
- `pip install strands-agents strands-agents-tools`, Python 3.10+; `from strands import Agent, tool`; `@tool` on a docstringed function; `agent("prompt")` returns an `AgentResult`. https://strandsagents.com/docs/user-guide/sdk/quickstart/python/ and https://github.com/strands-agents/sdk-python (README).
- Search results state Amazon Bedrock is the default provider; `BedrockModel(model_id=...)`. The fetched quickstart shows an `AnthropicModel`; the Bedrock provider page URL I tried 404'd. So `from strands.models import BedrockModel` and the `system_prompt=`, `region_name=`, `temperature=` kwargs in `_via_strands` are **TODO-verify**.

AgentCore
- Service list and Runtime properties: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html and https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.md

SageMaker RCF
- https://docs.aws.amazon.com/sagemaker/latest/dg/randomcutforest.html (unsupervised, scores; CSV/RecordIO; ml.c5 for inference; scores >3 std dev from mean commonly treated as anomalous).

EventBridge Scheduler
- cron/rate/one-time schedules, retries, flexible windows, at-least-once delivery, SQS/Lambda templated targets. https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html . Note at-least-once: our handlers are idempotent.

Lambda function URLs
- `AuthType` AWS_IAM or NONE; NONE = anyone can call, so validate in code (we HMAC-verify); reserved concurrency throttles (max RPS = 10 x reserved concurrency; excess gets HTTP 429; zero disables). https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html

## Not verified in docs (TODO-verify)
- IAM action names authorising Converse (`bedrock:InvokeModel`?) and ApplyGuardrail in the task role.
- `AWS::Scheduler::Schedule`, `AWS::Lambda::Url`, `ManageMasterUserPassword`: accepted by cfn-lint schemas, not deployed.
- Ring: the signature scheme and event envelope follow the Ring developer docs (`watch/ringapi.py`) but have not been exercised with live Ring traffic. Ring documents no battery webhook (battery is polled) and `device_online` can serve as a heartbeat.
- Live latency, cold starts, throttling, guardrail false-positive rate, real cost.

## Cost drivers (no prices verified)
- Bedrock: only calls that pass validation gates run: 1 opener per notification to a caregiver/person + 1 narrative per weekly email + advisor calls for ambiguous mornings only. Hard stop: `BEDROCK_DAILY_CALL_CAP` (default 200/day, counted in DB table `AiUsage` with token totals); outputs capped at 90-220 tokens. Worst case per day = cap x (short prompt + <= 220 output tokens).
- Lambda/SQS/Scheduler: one tick/minute = 43,200 scheduler invocations and SQS messages per month regardless of household count (TODO-verify their free tiers/pricing). If that matters, slow the tick to rate(5 minutes) (escalation granularity is 10+ minutes).
- RDS db.t4g.micro + 20 GB is the fixed floor of the stack (TODO-verify price). ECS Fargate web + worker are the other fixed cost. Reserved concurrency and the SQS DLQ bound abuse.
