import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import privacy

# Event kinds that prove the device is alive but say nothing about the person.
HEALTH_KINDS = {"device_status", "heartbeat", "battery", "offline"}


class Household(models.Model):
    """A person being cared for. We only store Ring event metadata: never video, images or audio."""
    name = models.CharField(max_length=100)
    ring_location_id = models.CharField(max_length=100, unique=True)
    window_start = models.TimeField(help_text="Morning activity usually starts after this")
    check_by = models.TimeField(help_text="If nothing has happened by this time, check in")
    adaptive = models.BooleanField(default=True, help_text="Learn the usual first-activity time and tune the deadline")
    person_telegram_chat_id = models.BigIntegerField(null=True, blank=True, editable=False,
                                                     help_text="Their own Telegram chat, for the gentle nudge (never shown)")
    nudge_enabled = models.BooleanField(default=False, help_text="Message them first, before anyone else")
    escalation_minutes = models.PositiveIntegerField(default=20, help_text="Wait this long between escalation steps")
    sensor_silent_hours = models.PositiveIntegerField(default=36, help_text="No events at all for this long = device problem")
    # Away / holiday mode and snooze
    away_from = models.DateField(null=True, blank=True)
    away_until = models.DateField(null=True, blank=True)
    snooze_until = models.DateTimeField(null=True, blank=True)
    # Seasonal awareness (optional, approximate location only)
    daylight_aware = models.BooleanField(default=False)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    retention_days = models.PositiveIntegerField(default=90, help_text="Delete event history older than this")
    consent_at = models.DateTimeField(null=True, blank=True, help_text="When the owner confirmed the person knows and agrees")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name

    def is_away(self, today=None):
        today = today or timezone.localdate()
        return bool(self.away_from and self.away_from <= today and (self.away_until is None or today <= self.away_until))

    def is_snoozed(self, now=None):
        return bool(self.snooze_until and self.snooze_until > (now or timezone.now()))


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField(max_length=40, blank=True, help_text="Include country code, e.g. +2348012345678")
    large_type = models.BooleanField(default=False, help_text="Larger text and higher contrast")
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, editable=False, help_text="Set by the bot link; never shown")


class Membership(models.Model):
    OWNER, CAREGIVER, VIEWER = "owner", "caregiver", "viewer"
    ROLES = [(OWNER, "Owner (manages everything)"), (CAREGIVER, "Caregiver (gets alerts, can acknowledge)"),
             (VIEWER, "Viewer (read-only, no alerts)")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=12, choices=ROLES, default=CAREGIVER)
    priority = models.PositiveIntegerField(default=1, help_text="1 = contacted first")
    notify_email = models.BooleanField(default=True)
    notify_telegram = models.BooleanField(default=True)
    notify_push = models.BooleanField(default=True)
    quiet_start = models.TimeField(null=True, blank=True, help_text="No phone alerts from this time...")
    quiet_end = models.TimeField(null=True, blank=True, help_text="...until this time (unless urgent)")
    weekly_summary = models.BooleanField(default=True)

    class Meta:
        unique_together = [("user", "household")]
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.user} @ {self.household} ({self.role})"

    @property
    def can_manage(self):
        return self.role == self.OWNER

    @property
    def can_act(self):
        return self.role in (self.OWNER, self.CAREGIVER)


class EmergencyContact(models.Model):
    """Not a Quiet Morning user: reached last, by Telegram/email, only after earlier steps go unanswered."""
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="emergency_contacts")
    name = models.CharField(max_length=100)
    relation = models.CharField(max_length=60, blank=True)
    phone = models.CharField(max_length=40, blank=True, help_text="For people to call; not used for messaging")
    email = models.EmailField(blank=True)
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, editable=False)


class Invite(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="invites")
    email = models.EmailField()
    role = models.CharField(max_length=12, choices=Membership.ROLES, default=Membership.CAREGIVER)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    def save(self, *a, **kw):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*a, **kw)

    @property
    def usable(self):
        return self.accepted_at is None and self.expires_at > timezone.now()


class Event(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=40)          # motion, ding, door_open, heartbeat ...
    device = models.CharField(max_length=100, blank=True)
    at = models.DateTimeField(db_index=True)
    raw = models.JSONField(default=dict)
    external_id = models.CharField(max_length=100, blank=True, help_text="Ring event id, or body hash; for idempotency")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["household", "external_id"],
                                               condition=~models.Q(external_id=""), name="uniq_event_per_household")]

    def save(self, *a, **kw):
        # Metadata-only guarantee, enforced at the model layer as well as the webhook.
        self.raw = privacy.sanitize_payload(self.raw)
        super().save(*a, **kw)

    @property
    def is_health(self):
        return self.kind in HEALTH_KINDS


class DeviceStatus(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="devices")
    device = models.CharField(max_length=100)
    last_seen = models.DateTimeField()
    battery = models.PositiveSmallIntegerField(null=True, blank=True)
    online = models.BooleanField(default=True)

    class Meta:
        unique_together = [("household", "device")]


class ExpectedLate(models.Model):
    """'Mum has a hospital appointment / lie-in today': move today's deadline, with a reason."""
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="expected_late")
    date = models.DateField()
    until_time = models.TimeField()
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        unique_together = [("household", "date")]


class CheckIn(models.Model):
    QUIET, SENSOR = "quiet", "sensor"
    OPEN, ACK, HELP, EXHAUSTED = "open", "acknowledged", "help_requested", "exhausted"
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="checkins")
    date = models.DateField()
    kind = models.CharField(max_length=10, default=QUIET)
    state = models.CharField(max_length=16, default=OPEN)
    level = models.PositiveSmallIntegerField(default=0, help_text="Escalation steps already taken")
    next_step_at = models.DateTimeField(null=True, blank=True)
    confidence = models.PositiveSmallIntegerField(default=0)
    explanation = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=40, blank=True)   # all_ok / needs_help / person_ok
    acknowledged_by = models.CharField(max_length=120, blank=True)
    advice = models.CharField(max_length=300, blank=True, help_text="Advisory note from the optional care-coordinator agent (never binding)")

    class Meta:
        unique_together = [("household", "date", "kind")]

    @property
    def is_open(self):
        return self.state == self.OPEN


class Notification(models.Model):
    """Audit trail: one row per attempted message to one person on one channel."""
    checkin = models.ForeignKey(CheckIn, on_delete=models.CASCADE, related_name="notifications")
    step = models.PositiveSmallIntegerField(default=0)
    step_kind = models.CharField(max_length=12, blank=True)       # nudge / caregiver / emergency / info
    recipient = models.CharField(max_length=120)
    destination = models.CharField(max_length=120, blank=True)   # masked
    channel = models.CharField(max_length=12)
    status = models.CharField(max_length=12, default="pending")  # sent / stubbed / retrying / failed / skipped / deferred
    detail = models.CharField(max_length=300, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    ack_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    contact = models.ForeignKey(EmergencyContact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    text = models.TextField(blank=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


def _link_token():
    return secrets.token_urlsafe(24)   # 32 chars of A-Za-z0-9_-, valid as a Telegram /start payload (max 64)


class TelegramLink(models.Model):
    """One-time deep-link token binding a Telegram chat to a caregiver, an emergency contact or the person themselves."""
    token = models.CharField(max_length=64, unique=True, default=_link_token, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    contact = models.ForeignKey(EmergencyContact, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    household = models.ForeignKey(Household, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
                                  help_text="Set when the link is for the person being cared for")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def save(self, *a, **kw):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*a, **kw)

    @property
    def usable(self):
        return self.used_at is None and self.expires_at > timezone.now()


class PushSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)


class AiUsage(models.Model):
    """Per-day counter for optional model calls, used for the hard cost cap (see watch/aws/bedrock.py)."""
    date = models.DateField(unique=True)
    calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
