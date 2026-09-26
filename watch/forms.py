import uuid
from datetime import time

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import EmergencyContact, ExpectedLate, Household, Invite, Membership, Profile

T = lambda: forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
User = get_user_model()


class SignupForm(UserCreationForm):
    email = forms.EmailField()

    class Meta:
        model = User
        fields = ["username", "email", "first_name"]

    def save(self, commit=True):
        u = super().save(commit=False)
        u.email = self.cleaned_data["email"]
        if commit:
            u.save()
            Profile.objects.get_or_create(user=u)
        return u


class PersonForm(forms.ModelForm):
    ring_location_id = forms.CharField(required=False, help_text="From your Ring account. Leave blank to connect later.")

    class Meta:
        model = Household
        fields = ["name", "ring_location_id", "person_phone", "nudge_enabled"]
        labels = {"name": "Who are you caring for?", "person_phone": "Their phone (optional)",
                  "nudge_enabled": "Send them a gentle hello first, before alerting anyone"}

    def clean_ring_location_id(self):
        v = self.cleaned_data.get("ring_location_id") or f"qm-{uuid.uuid4().hex[:10]}"
        if Household.objects.filter(ring_location_id=v).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("That Ring location is already connected to a household.")
        return v


class MorningForm(forms.ModelForm):
    window_start = forms.TimeField(widget=T(), label="Their day usually starts after")
    check_by = forms.TimeField(widget=T(), label="If nothing has happened by, check in")

    class Meta:
        model = Household
        fields = ["window_start", "check_by", "adaptive", "daylight_aware"]
        labels = {"adaptive": "Learn their routine and adjust (per weekday)",
                  "daylight_aware": "Account for sunrise changing through the year (needs location in Settings)"}

    def clean(self):
        d = super().clean()
        if d.get("window_start") and d.get("check_by") and d["check_by"] <= d["window_start"]:
            raise forms.ValidationError("Check-in time must be after the day starts.")
        return d


class SettingsForm(MorningForm):
    class Meta(MorningForm.Meta):
        fields = ["window_start", "check_by", "adaptive", "daylight_aware", "latitude", "longitude", "person_phone",
                  "nudge_enabled", "escalation_minutes", "sensor_silent_hours", "retention_days"]
        labels = {**MorningForm.Meta.labels, "escalation_minutes": "Minutes between escalation steps",
                  "sensor_silent_hours": "Hours with no events at all before we suspect the device",
                  "retention_days": "Keep event history for (days)", "person_phone": "Their phone (for the gentle hello)",
                  "nudge_enabled": "Send them a gentle hello first"}

    def clean_retention_days(self):
        v = self.cleaned_data["retention_days"]
        if not 14 <= v <= 365:
            raise forms.ValidationError("Choose between 14 and 365 days (learning needs a few weeks).")
        return v

    def clean_escalation_minutes(self):
        v = self.cleaned_data["escalation_minutes"]
        if not 1 <= v <= 240:
            raise forms.ValidationError("Choose between 1 and 240 minutes.")
        return v

    def clean(self):
        d = super().clean()
        for f, lim in (("latitude", 90), ("longitude", 180)):
            if d.get(f) is not None and abs(d[f]) > lim:
                self.add_error(f, "Out of range.")
        return d


class ExpectedLateForm(forms.ModelForm):
    until_time = forms.TimeField(widget=T(), label="Expect them by")

    class Meta:
        model = ExpectedLate
        fields = ["until_time", "note"]
        labels = {"note": "Why (optional)"}


class AwayForm(forms.Form):
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}), label="Back on (optional)")


class PrefsForm(forms.ModelForm):
    phone = forms.CharField(required=False, label="Your phone (for SMS/WhatsApp)")
    large_type = forms.BooleanField(required=False, label="Large type and high contrast")
    quiet_start = forms.TimeField(required=False, widget=T(), label="Quiet hours start")
    quiet_end = forms.TimeField(required=False, widget=T(), label="Quiet hours end")

    class Meta:
        model = Membership
        fields = ["notify_email", "notify_push", "notify_sms", "notify_whatsapp", "quiet_start", "quiet_end", "weekly_summary"]
        labels = {"notify_email": "Email", "notify_push": "Push notification (this device)", "notify_sms": "SMS",
                  "notify_whatsapp": "WhatsApp", "weekly_summary": "Weekly summary email"}


class EmergencyContactForm(forms.ModelForm):
    class Meta:
        model = EmergencyContact
        fields = ["name", "relation", "phone", "email"]

    def clean(self):
        d = super().clean()
        if not (d.get("phone") or d.get("email")):
            raise forms.ValidationError("Add a phone number or an email so we can reach them.")
        return d


class InviteForm(forms.ModelForm):
    class Meta:
        model = Invite
        fields = ["email", "role"]
