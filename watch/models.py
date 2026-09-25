import uuid

from django.db import models


class Household(models.Model):
    """A person being cared for. We only store Ring event metadata — never video."""
    name = models.CharField(max_length=100)
    ring_location_id = models.CharField(max_length=100, unique=True)
    window_start = models.TimeField(help_text="Morning activity usually starts after this")
    check_by = models.TimeField(help_text="If nothing has happened by this time, check in")
    caregiver_contact = models.CharField(max_length=200, help_text="Phone/WhatsApp/email")
    caregiver_email = models.EmailField(blank=True)
    adaptive = models.BooleanField(default=True, help_text="Learn the usual first-activity time and tune the deadline")
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
                                    help_text="Secret in the caregiver dashboard URL")

    def __str__(self):
        return self.name


class Event(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=40)          # motion, ding, door_open, ...
    device = models.CharField(max_length=100, blank=True)
    at = models.DateTimeField(db_index=True)
    raw = models.JSONField(default=dict)
    external_id = models.CharField(max_length=100, blank=True, help_text="Ring event id, or body hash; for idempotency")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["household", "external_id"],
                                               condition=~models.Q(external_id=""), name="uniq_event_per_household")]


class CheckIn(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="checkins")
    date = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=40, blank=True)   # all_ok / needs_help

    class Meta:
        unique_together = [("household", "date")]
