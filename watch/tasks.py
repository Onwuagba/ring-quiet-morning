from celery import shared_task
from django.utils import timezone

from .models import CheckIn, Household
from .notify import send_check_in
from .rules import is_quiet


@shared_task
def check_quiet_mornings():
    now = timezone.now()
    sent = 0
    for h in Household.objects.all():
        if is_quiet(h, now):
            c = CheckIn.objects.create(household=h, date=timezone.localtime(now).date())
            send_check_in(h, c)
            sent += 1
    return sent
