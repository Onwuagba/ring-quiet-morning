"""Metadata-only guarantee, enforced in code.

We keep an ALLOWLIST of small scalar fields. Anything else (media URLs, images, audio, nested blobs,
anything that looks like a URL) is dropped before it can reach the database. The privacy page in the UI is
generated from the same constants, so what we promise and what we enforce cannot drift apart.
"""
import re

# Keys we are willing to store. Names other than event_type/created_at/location_id/device_id are our own
# generic schema; TODO-verify against the real Ring payload before relying on them.
ALLOWED_KEYS = ("event_id", "id", "event_type", "device_id", "location_id", "created_at", "battery", "status", "online")
NEVER_STORED = ("video", "video_url", "snapshot_url", "image", "thumbnail", "audio", "recording", "stream", "face", "transcript")
_URL = re.compile(r"^\s*(https?:)?//|^data:", re.I)
MAX_STR = 120


def sanitize_payload(payload) -> dict:
    """Return only allowlisted, short, non-URL scalar values."""
    if not isinstance(payload, dict):
        return {}
    clean = {}
    for k, v in payload.items():
        if k not in ALLOWED_KEYS:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            clean[k] = v
        elif isinstance(v, str) and len(v) <= MAX_STR and not _URL.match(v):
            clean[k] = v
    return clean


GUARANTEES = [
    "We store only the type of event (motion, doorbell, door), which device, and when.",
    "We never request, receive, store or display video, snapshots, audio or faces.",
    "Any media field that arrives is dropped before saving. This is enforced in code and tested.",
    "Only the caregivers you invite can see the timeline. You can remove anyone at any time.",
    "You can download everything we hold, or delete it all, from Settings.",
    "Event history is deleted automatically after the retention period you choose.",
]
