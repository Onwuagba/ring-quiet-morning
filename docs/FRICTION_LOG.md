# Friction log and feature requests

Entries come from reading the documentation and offline work. No live Ring account or AWS account was used, so there are no claims about live service behaviour.

## Friction log

**F1. Finding the right Bedrock model id**
- Task: call Converse. Steps: followed Converse page to model cards; Nova Lite (in-region plus `us.`/`eu.` profiles, no global) vs Claude Haiku 4.5 (inference profile required on `bedrock-runtime`).
- Expected: one string per model and Region. Actual: several pages. Severity: medium. Workaround: no default `BEDROCK_MODEL_ID` shipped; operator sets it. Suggestion: one-line "use this string in region X".

**F2. Moved or stub documentation pages**
- Task: follow older links. Actual: `conversation-inference-call.html` and `models-supported.html` return only "content has moved". Severity: low. Workaround: follow links. Suggestion: redirects.

**F3. ApplyGuardrail page inconsistencies**
- Task: use standalone ApplyGuardrail. Actual: `output` vs `outputs`, `topicPolicyUnits` vs `topicPolicyUnitsProcessed`, invalid example JSON. Severity: medium. Workaround: used `guardrailConfig` on Converse. Suggestion: fix examples against the schema.

**F4. Guardrails + Converse footgun**
- Task: use guardrails with tools. Expected: all content evaluated. Actual: with any `guardContent` only those blocks are evaluated; tool inputs/results never. Severity: medium (safety). Workaround: validate output in code. Suggestion: prominent warning.

**F5. Strands docs**
- Task: import `BedrockModel`. Actual: quickstart shows `AnthropicModel`; Bedrock provider page 404. Severity: low. Workaround: left TODO-verify. Suggestion: link the Bedrock provider page from the quickstart.

**F6. Ring webhook details (docs reading)**
- Task: verify signatures and device health. Actual: signature header and envelope are documented, but no timestamp header, no location id, and no battery webhook (battery needs polling the status endpoint). Severity: medium for absence-based products. Workaround: `meta.time` for replay window, `poll_ring_status` for battery, own simulator. Suggestion: add a signed timestamp header and a battery-low event.

## Feature requests

1. **Device health events (heartbeat, last-seen, battery, offline) in the Ring API.** Why: separates "quiet morning" from "sensor down" and is the biggest lever against false alarms. Urgency: critical.
2. **A "presence metadata only, never media" scope.** Why: suits caregiving apps and clarifies what is shared under video E2EE. Urgency: important.
3. **A signed timestamp header on webhooks.** Why: exact replay protection (today only `meta.time` is available). Urgency: important.
4. **Sandbox scenarios with a controllable clock** (quiet-day, late-day, offline). Why: lets absence-based products be tested against Ring itself, not only our own simulator. Urgency: important.
5. **Consent and transparency for the monitored person.** Why: they should see who has access. Urgency: important.
6. **A single "call this model with Converse in region X" string per model card.** Why: saves several page hops. Urgency: nice-to-have.
