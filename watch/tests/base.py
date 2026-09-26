import hashlib
import hmac
import json
from datetime import datetime, time, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from watch.models import EmergencyContact, Event, Household, Membership, Profile

User = get_user_model()
PW = "correct-horse-battery-9"


def today():
    return timezone.localdate()


def at(h, m=0, days_ago=0):
    return timezone.make_aware(datetime.combine(today() - timedelta(days=days_ago), time(h, m)))


def sign(body: bytes) -> str:
    return hmac.new(settings.RING_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def seed_history(h, first=(6, 30), days=8, heartbeat=True):
    for i in range(1, days + 1):
        Event.objects.create(household=h, kind="motion", device="kitchen", at=at(*first, days_ago=i))
        if heartbeat:
            Event.objects.create(household=h, kind="heartbeat", device="doorbell", at=at(12, 0, days_ago=i))


def make_user(name, email=None, **profile):
    u = User.objects.create_user(name, email or f"{name}@example.com", PW, first_name=name.title())
    Profile.objects.create(user=u, **profile)
    return u


class Base(TestCase):
    """Household 'Mama' with owner amina (priority 1), caregiver tunde (2) and an emergency contact."""

    def setUp(self):
        self.h = Household.objects.create(name="Mama", ring_location_id="loc1", window_start=time(5), check_by=time(9),
                                          person_phone="+234111", escalation_minutes=20)
        self.amina = make_user("amina", phone="+234222")
        self.tunde = make_user("tunde", phone="+234333")
        self.ma = Membership.objects.create(user=self.amina, household=self.h, role="owner", priority=1)
        self.mt = Membership.objects.create(user=self.tunde, household=self.h, role="caregiver", priority=2)
        self.ec = EmergencyContact.objects.create(household=self.h, name="Aunty", phone="+234444", email="aunty@example.com")

    def login(self, user=None):
        self.client.login(username=(user or self.amina).username, password=PW)
