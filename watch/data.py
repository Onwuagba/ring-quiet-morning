"""Household data export and deletion (privacy)."""
from django.utils import timezone


def export_household(h) -> dict:
    return {
        "exported_at": timezone.now().isoformat(),
        "note": "Everything Quiet Morning holds about this household. Metadata only; no video, images or audio exist.",
        "household": {"name": h.name, "ring_location_id": h.ring_location_id, "window_start": str(h.window_start),
                      "check_by": str(h.check_by), "adaptive": h.adaptive, "retention_days": h.retention_days,
                      "away_from": str(h.away_from or ""), "away_until": str(h.away_until or ""),
                      "person_telegram_connected": bool(h.person_telegram_chat_id)},
        "members": [{"username": m.user.username, "email": m.user.email, "role": m.role, "priority": m.priority}
                    for m in h.memberships.select_related("user")],
        "emergency_contacts": [{"name": c.name, "relation": c.relation, "phone": c.phone, "email": c.email}
                               for c in h.emergency_contacts.all()],
        "events": [{"kind": e.kind, "device": e.device, "at": e.at.isoformat(), "external_id": e.external_id, "raw": e.raw}
                   for e in h.events.order_by("at")],
        "checkins": [{"date": str(c.date), "kind": c.kind, "state": c.state, "resolution": c.resolution,
                      "acknowledged_by": c.acknowledged_by, "explanation": c.explanation, "confidence": c.confidence,
                      "notifications": [{"recipient": n.recipient, "channel": n.channel, "status": n.status,
                                         "detail": n.detail, "at": n.created_at.isoformat()} for n in c.notifications.all()]}
                     for c in h.checkins.prefetch_related("notifications")],
    }


def delete_household(h):
    """Cascade-deletes events, check-ins, notifications, members' links, invites, devices, contacts."""
    h.delete()
