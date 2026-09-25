from django.contrib import admin

from .models import CheckIn, Event, Household

admin.site.register([Household, Event, CheckIn])
