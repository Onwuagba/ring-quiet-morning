from django.contrib import admin

from . import models

for m in (models.Household, models.Membership, models.EmergencyContact, models.Invite, models.CheckIn,
          models.Notification, models.DeviceStatus, models.ExpectedLate):
    admin.site.register(m)
