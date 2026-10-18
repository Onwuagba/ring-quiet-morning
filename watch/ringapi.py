"""Ring Partner API: webhook verification, event parsing and the device-status client.

Facts below come from the Ring developer documentation (developer.amazon.com/docs/ring/api-documentation.html and the
release notes), read 2026-10-18:

* Webhooks carry `X-Signature: sha256=<hex>`, an HMAC-SHA256 of the RAW request body with the partner's signing key.
  No timestamp header is documented, so replay protection uses `meta.time` (when Ring sent the webhook).
* The body is `{"meta": {"version", "time", "request_id", "account_id"}, "data": {"id", "type", "attributes": {"source",
  "source_type", "timestamp" (epoch ms), "sub_type"?, "component_ids"?}, "relationships": {...}}}`.
* Event types: motion_detected, button_press, device_online, device_offline, device_added, device_removed,
  app_integration_added/removed, subscription_activated/deactivated. Ring sends NO battery webhook; battery comes
  from `GET /v1/devices/{id}/status` -> `battery_status.percentage`.
* API host https://api.amazonvision.com, OAuth token endpoint https://oauth.ring.com/oauth/token.
* The webhook envelope has no location id: households are matched on `meta.account_id`
  (stored in Household.ring_location_id).

Metadata only: nothing here reads media; `sub_type` (human/vehicle/...) is the only extra attribute kept."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone as dt_timezone

API_BASE = "https://api.amazonvision.com"
TOKEN_URL = "https://oauth.ring.com/oauth/token"

# Ring event type -> Quiet Morning event kind. Anything that is not the resident moving about is a non-activity kind
# (see models.HEALTH_KINDS): a visitor pressing the doorbell or a billing event must never count as "they are up".
KIND_BY_TYPE = {
    "motion_detected": "motion",
    "button_press": "doorbell_press",
    "device_online": "heartbeat",
    "device_offline": "offline",
    "device_added": "device_status",
    "device_removed": "device_status",
    "app_integration_added": "device_status",
    "app_integration_removed": "device_status",
    "subscription_activated": "device_status",
    "subscription_deactivated": "device_status",
}


def sign(secret: str, raw: bytes) -> str:
    """The `X-Signature` header value for `raw`."""
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify(header: str, raw: bytes, secret: str) -> bool:
    """Constant-time check of `X-Signature` (`sha256=<hex>`; a bare hex digest is tolerated). Never raises."""
    if not secret or not header:
        return False
    given = header.strip()
    if given.lower().startswith("sha256="):
        given = given[7:]
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given.lower(), expected)


def parse_time(value) -> datetime | None:
    """`meta.time` looks like 2026-02-13T13:39:57.200155525Z (nanoseconds). Returns an aware UTC datetime or None."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    if "." in s:  # trim fractional seconds to microseconds, keep the offset
        head, rest = s.split(".", 1)
        frac = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            frac += rest[i]
            i += 1
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest[i:]}"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt_timezone.utc)


def from_epoch_ms(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=dt_timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


class Event:
    """A normalised Ring (or legacy flat) event."""
    __slots__ = ("account", "type", "kind", "device", "at", "sent_at", "event_id", "sub_type", "extra")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def stored(self) -> dict:
        """What is allowed into Event.raw (privacy.sanitize_payload filters again)."""
        d = {"event_type": self.type, "device_id": self.device, "event_id": self.event_id}
        if self.sub_type:
            d["sub_type"] = self.sub_type
        if self.at:
            d["created_at"] = self.at.isoformat()
        d.update(self.extra or {})
        return d


def parse(body) -> Event | None:
    """Normalise a decoded webhook body. Ring's envelope is the primary format; the older flat
    {location_id, event_type, device_id, created_at, event_id} shape is still accepted (used by early tooling)."""
    if not isinstance(body, dict):
        return None
    data, meta = body.get("data"), body.get("meta")
    if isinstance(data, dict) and isinstance(meta, dict):
        attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
        etype = str(data.get("type", "unknown"))[:40]
        sub = attrs.get("sub_type")
        return Event(account=meta.get("account_id") if isinstance(meta.get("account_id"), str) else None, type=etype,
                     kind=KIND_BY_TYPE.get(etype, "device_status"), device=str(attrs.get("source", ""))[:100],
                     at=from_epoch_ms(attrs.get("timestamp")), sent_at=parse_time(meta.get("time")),
                     event_id=str(data.get("id") or meta.get("request_id") or "")[:100],
                     sub_type=sub[:30] if isinstance(sub, str) else None, extra={})
    if isinstance(body.get("location_id"), str):
        etype = str(body.get("event_type", "unknown"))[:40]
        when = parse_time(body.get("created_at")) if body.get("created_at") else None
        extra = {k: body[k] for k in ("battery", "online") if k in body}
        return Event(account=body["location_id"], type=etype, kind=etype, device=str(body.get("device_id", ""))[:100],
                     at=when, sent_at=when, event_id=str(body.get("event_id") or body.get("id") or "")[:100],
                     sub_type=None, extra=extra)
    return None


def build_event(account_id: str, etype: str, device: str, at: datetime | None = None, sub_type: str | None = None,
                now: datetime | None = None) -> dict:
    """A webhook body in Ring's documented envelope (used by the simulator and the tests)."""
    now = now or datetime.now(dt_timezone.utc)
    at = at or now
    ms = int(at.timestamp() * 1000)
    attrs = {"source": device, "source_type": "devices", "timestamp": ms}
    if sub_type:
        attrs["sub_type"] = sub_type
    return {"meta": {"version": "1.1", "time": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "request_id": str(uuid.uuid4()),
                     "account_id": account_id},
            "data": {"id": f"{device}_{etype}_{ms}", "type": etype, "attributes": attrs,
                     "relationships": {"devices": {"links": {"self": f"/v1/devices/{device}"}}}}}


class RingClient:
    """Minimal client for the Ring Partner API (device status polling). `opener` is injectable for tests."""

    def __init__(self, access_token: str, base: str = API_BASE, opener=None):
        self.token, self.base = access_token, base.rstrip("/")
        self._open = opener or urllib.request.urlopen

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        with self._open(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def devices(self) -> list[dict]:
        return self._get("/v1/devices").get("data", [])

    def status(self, device_id: str) -> dict:
        return self._get(f"/v1/devices/{urllib.parse.quote(device_id, safe='')}/status")

    @staticmethod
    def battery_and_online(status: dict) -> tuple[int | None, bool | None]:
        """(battery %, online). Ring reports 255 (or omits the field) for devices without a battery; gate on `online`."""
        s = status.get("data", status) if isinstance(status, dict) else {}
        s = s.get("attributes", s) if isinstance(s, dict) else {}
        online = s.get("online") if isinstance(s.get("online"), bool) else None
        pct = (s.get("battery_status") or {}).get("percentage") if isinstance(s.get("battery_status"), dict) else None
        if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
            pct = None
        return pct, online


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str, opener=None) -> dict:
    """OAuth 2.0 refresh at oauth.ring.com (standard grant_type=refresh_token)."""
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                   "client_id": client_id, "client_secret": client_secret}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with (opener or urllib.request.urlopen)(req, timeout=10) as r:
        return json.loads(r.read().decode())


def now_epoch() -> float:
    return time.time()
