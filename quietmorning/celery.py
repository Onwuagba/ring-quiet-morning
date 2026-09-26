import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quietmorning.settings")
app = Celery("quietmorning")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.timezone = os.getenv("TIME_ZONE", "Africa/Lagos")
app.conf.beat_schedule = {
    "tick": {"task": "watch.tasks.tick", "schedule": 60.0},                       # assess, escalate, retry
    "purge": {"task": "watch.tasks.purge_old_data", "schedule": crontab(hour=3, minute=30)},
    "weekly-summary": {"task": "watch.tasks.send_weekly_summaries", "schedule": crontab(day_of_week=1, hour=8, minute=0)},
}
