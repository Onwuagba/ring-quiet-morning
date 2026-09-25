# Feedback for Amazon / Ring (draft, from building this)

Written from a scaffold built without live Ring access; items marked "unverified" need checking against the real docs/sandbox.

- Webhook signing: we could not confirm the signature header or algorithm, so our verifier is a placeholder (unverified). A documented scheme with a timestamp component would make replay protection straightforward.
- Event ids: a stable event id in every payload would allow exact idempotent handling; we fall back to hashing the body.
- Metadata under E2EE: unclear which motion/door events reach third parties when video end-to-end encryption is on. A "presence metadata only" scope that never exposes media would suit caregiving apps.
- Absence-based use cases: a "device heartbeat / last seen" signal would let apps distinguish "quiet home" from "offline device", which is our biggest false-alarm source.
- Consent: a first-class flow for the monitored person (not just the account owner) to approve a caregiver integration.
- Sandbox: simulated event streams for testing quiet-day scenarios.
