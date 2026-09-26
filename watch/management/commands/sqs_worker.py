from django.core.management.base import BaseCommand, CommandError

from watch.aws import queue


class Command(BaseCommand):
    help = "Long-poll SQS_QUEUE_URL and run scheduled jobs / queued Ring events (AWS-native alternative to Celery)."

    def add_arguments(self, p):
        p.add_argument("--once", action="store_true", help="poll a single time and exit")

    def handle(self, *a, **o):
        from django.conf import settings
        if not settings.SQS_QUEUE_URL:
            raise CommandError("Set SQS_QUEUE_URL")
        while True:
            n = queue.poll_once()
            if o["once"]:
                self.stdout.write(f"handled {n}")
                return
