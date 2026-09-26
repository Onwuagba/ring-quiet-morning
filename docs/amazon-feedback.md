# Feedback for Amazon / Ring (draft, from building this)

Written without live Ring access; items marked "unverified" need checking against the real docs and sandbox.

## What blocked or slowed us
- **Webhook signing** (unverified): we could not confirm the signature header or algorithm, so our verifier is a labelled placeholder. A documented HMAC scheme with a signed timestamp would make replay protection exact instead of trusting `created_at`.
- **Stable event ids** in every payload would allow exact idempotent handling; we fall back to hashing the body.
- **Device health signals**: a heartbeat / last-seen / battery / offline event is the single biggest lever against false alarms. Absence-based products cannot tell "quiet home" from "dead doorbell" without it. We built the whole sensor-health path against a generic schema of our own.
- **Metadata under E2EE** (unverified): unclear which motion/door events reach third parties when video end-to-end encryption is on. A "presence metadata only, never media" OAuth scope would suit caregiving apps and reassure families.
- **Consent for the monitored person**: a first-class flow for the person being cared for (not just the account owner) to approve a caregiver integration, and to see who has access.
- **Sandbox**: simulated event streams for quiet-day, late-day and offline-device scenarios, with a controllable clock.
- **Event granularity**: knowing *which* device (front door vs kitchen) and event type lets routines be far more specific; also useful: a coarse "activity" event that is not tied to a camera zone.

## Why this matters
Families of older adults are a large group who would use Ring for reassurance rather than security. A metadata-only, absence-based
API tier would make privacy-preserving products like this straightforward and safe to build.
