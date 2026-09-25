import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quietmorning.settings")
app = Celery("quietmorning")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.beat_schedule = {
    "check-quiet-mornings": {"task": "watch.tasks.check_quiet_mornings", "schedule": 300.0},  # every 5 min
}
