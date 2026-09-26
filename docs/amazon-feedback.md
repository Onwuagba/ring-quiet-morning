# Feedback on Amazon tools (Ring, AWS) : structure for the submission

Split into **observed while building** (facts I hit, with where) and **live experience** (placeholders: fill in after running against real AWS/Ring; nothing below "live" has been done).

## A. Ring (unverified, no Ring access during the build)
- Webhook signing: could not confirm header/algorithm; our verifier is a labelled placeholder (`watch/webhook.py`, `infra/lambda_ingest.py`). A documented HMAC over body + signed timestamp would make replay protection exact.
- Stable event ids in every payload (idempotent handling; we hash the body today).
- Device health (heartbeat, last-seen, battery, offline): the biggest lever against false alarms for any absence-based product.
- Metadata under video E2EE: which motion/door events reach third parties? A "presence metadata only, never media" scope would suit caregiving apps.
- Consent for the monitored person (not only the account owner) and a way for them to see who has access.
- Sandbox with simulated quiet-day / late-day / offline scenarios and a controllable clock.
- Which device and event type, plus a coarse "activity" event not tied to a camera zone.

## B. AWS docs and API friction I actually observed (reading docs, 2026-09-26)
1. **Bedrock: moved/stub pages.** `conversation-inference-call.html` and `models-supported.html` return only "content has moved" notices. Followed links worked, but old links from blogs/search land on dead ends.
2. **Bedrock: model ID is not one thing.** Nova Lite has an in-region ID plus `us.`/`eu.` geo profiles, no global; Claude Haiku 4.5 on `bedrock-runtime` *requires* an inference profile and the bare ID is only for the separate `bedrock-mantle` endpoint. Nova Lite's card lists "structured outputs" as not supported while Haiku 4.5 supports them. It took several pages to learn which ID string to put in `modelId`; a single "call this model with Converse in region X: use this string" line would help.
3. **Bedrock: lifecycle dates that read as contradictory.** Nova Lite card: "EOL no sooner than Dec 05, 2025" yet lifecycle "Active" (today is Sept 2026). Haiku 4.5: "EOL no sooner than Oct 16, 2026", i.e. weeks away. "No sooner than" is hard to plan against; an actual announced date or "none announced" would be clearer.
4. **Guardrails `ApplyGuardrail` page internal inconsistencies.** The response schema shows `output` while the examples show `outputs`; usage fields are `topicPolicyUnits` in one place and `topicPolicyUnitsProcessed` in another; the example request JSON is not valid JSON (`"content": [ "text": {...} ]`). I avoided the standalone API and used `guardrailConfig` on Converse for that reason.
5. **Guardrails + Converse footgun.** Once any `guardContent` block appears, only those blocks are evaluated, everything else is skipped; and `guardrailConfig` does not evaluate tool inputs/results. Documented, but easy to miss and security-relevant for anyone mixing tools with guardrails. I deliberately do not use `guardContent` and validate output in code as well.
6. **Strands docs.** The Python quickstart's model example uses `AnthropicModel` with `claude-sonnet-5`, while other sources say Bedrock is the default provider; the Bedrock provider and structured-output pages I tried returned 404 (paths may have moved). The exact `BedrockModel` import path and kwargs therefore stayed TODO-verify in my code.
7. **AgentCore.** The overview lists 13 modular services (Harness, Runtime with microVMs vs Instances and a V2 platform note, Memory, Gateway, Identity, Policy, Registry, Payments...). Powerful, but as a newcomer deciding *whether* an event-driven, stateless, single-shot advisor needs it, I found no "when you do not need AgentCore" guidance. I decided from the microVM/session model that it does not (AWS_STACK.md).
8. **SageMaker RCF.** Clear algorithm page, but no guidance on minimum series length or on very low-dimensional data; the SDK v3 note (`ModelTrainer`, `Channel`) means older tutorials do not match.
9. **Lambda function URLs.** Good, concise. Missing in one place: what to do when Ring-like third parties cannot use SigV4 (answer: `NONE` + HMAC in code + reserved concurrency), which I had to infer.
10. **Positive.** The Converse response envelope (`output`, `stopReason`, `usage`) is uniform across models and made a small fail-safe wrapper easy; `guardrail_intervened` as a `stopReason` is an unambiguous fallback signal. Model cards with regional/profile tables are very useful once found. `cfn-lint` catches property mistakes offline.

## C. Live experience (fill in after real runs)
- Bedrock: model chosen: ____ ; region: ____ ; time to first successful `converse`: ____ ; access-enablement friction: ____ ; latency p50/p95: ____ ; guardrail false positives on wording: ____ ; real cost for a week: ____
- Strands: install/version: ____ ; did `_via_strands` import paths work as written? ____ ; agent latency: ____
- AgentCore: (not used) would you have used it now that you have seen it live? ____
- SQS/Lambda/Scheduler/RDS deploy: `cfn deploy` errors hit: ____ ; permission surprises (esp. Converse IAM action): ____
- Ring sandbox/API: signature scheme: ____ ; heartbeat/battery events exist? ____ ; E2EE metadata: ____
- What would have saved the most time: ____
