"""Optional Bedrock-written wording. The model only ever writes a short warm *opener* (or a weekly narrative
paragraph) from structured facts; everything safety-critical (what happened, confidence, "not an emergency",
the acknowledge link, emergency-contact and help-requested messages, device notices) stays deterministic and
is appended by the caller. Output is validated; any doubt returns "" and the caller uses the plain template."""
import logging
import re

from django.conf import settings

from . import bedrock

log = logging.getLogger("watch.wording")

# Never let generated text sound like medical judgment, invent urgency, or carry links/contact details.
BANNED = re.compile(
    r"diagnos|medical|medicine|doctor|stroke|dementia|fall(?:en|s)?\b|fell\b|injur|collaps|unconscious|dead|died|death|"
    r"ambulance|911|999|112|hospital|emergency|urgent|danger|critical|serious|alarm|panic|"
    r"call the police|police|guarantee|definitely|certainly|http|www\.|@|\.com|\n|<|>|`|\*|#", re.I)

SYSTEM_OPENER = (
    "You write one or two short, warm sentences that open a check-in message from a home-routine app to a family "
    "caregiver or to an older person. Use ONLY the facts provided. Do not give medical opinions, do not diagnose, "
    "do not speculate about causes, do not mention emergencies, do not include links, phone numbers or email "
    "addresses, and do not include any digits other than times given in the facts. Plain text, under 40 words.")

SYSTEM_WEEKLY = (
    "You write a calm 2 to 3 sentence summary of a person's week for a family caregiver from the structured facts "
    "provided. Use ONLY the facts. No medical opinions, no diagnosis, no speculation about causes, no advice beyond "
    "'a friendly call may be nice' when drift is present, no links or contact details, no digits other than numbers "
    "in the facts. Plain text, under 70 words.")


def validate(text, facts_text, max_len):
    """Return the cleaned text if it passes every check, else ''."""
    if not text:
        return ""
    if BANNED.search(text.strip()):       # checked on the raw text so multi-line output is rejected
        return ""
    t = " ".join(text.split()).strip().strip('"')
    if not t or len(t) > max_len:
        return ""
    allowed = set(re.findall(r"\d+", facts_text))
    if any(d not in allowed for d in re.findall(r"\d+", t)):
        return ""
    return t


def _enabled():
    return settings.BEDROCK_WORDING and bedrock.enabled()


def _facts_text(facts: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in facts.items() if v not in (None, ""))


def opener(kind, facts, client=None) -> str:
    """kind: 'caregiver' | 'nudge'. facts: first name / household label, usual time, hours late (no PII beyond that)."""
    if not _enabled():
        return ""
    ft = _facts_text(facts)
    out = bedrock.generate(SYSTEM_OPENER, f"Message type: {kind}\n{ft}", max_tokens=90, client=client)
    return validate(out, ft, 240)


def weekly_narrative(name, summary, client=None) -> str:
    if not _enabled():
        return ""
    facts = {"person": name, "mornings_with_activity_of_7": summary["days_seen"], "average_first_activity": summary["avg_first"],
             "quiet_morning_checkins": summary["quiet_count"], "device_notices": summary["sensor_count"],
             "average_minutes_to_confirm": summary["avg_response_min"], "gentle_drift_note": summary["drift"]}
    ft = _facts_text(facts)
    out = bedrock.generate(SYSTEM_WEEKLY, ft, max_tokens=160, client=client)
    return validate(out, ft, 600)
