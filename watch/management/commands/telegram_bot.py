"""Telegram bot setup.
  telegram_bot --set-webhook https://your.domain     register <url>/webhooks/telegram/ with the secret header (production)
  telegram_bot --delete-webhook                      remove the webhook
  telegram_bot --info                                show webhook status (URL and errors only)
  telegram_bot --poll                                dev mode: long-poll getUpdates, no HTTPS needed (removes any webhook)"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from watch import telegram as tg, tgbot


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, p):
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--set-webhook", metavar="BASE_URL")
        g.add_argument("--delete-webhook", action="store_true")
        g.add_argument("--info", action="store_true")
        g.add_argument("--poll", action="store_true")
        p.add_argument("--once", action="store_true", help="with --poll: fetch one batch and exit")

    def handle(self, *a, **o):
        if not settings.TELEGRAM_BOT_TOKEN and not tg._transport:
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")
        try:
            if o["set_webhook"]:
                if not settings.TELEGRAM_WEBHOOK_SECRET:
                    raise CommandError("Set TELEGRAM_WEBHOOK_SECRET first (1-256 chars of A-Z a-z 0-9 _ -).")
                url = o["set_webhook"].rstrip("/") + "/webhooks/telegram/"
                if not url.startswith("https://"):
                    raise CommandError("Telegram webhooks require an https:// URL. For local use run with --poll.")
                tg.api("setWebhook", {"url": url, "secret_token": settings.TELEGRAM_WEBHOOK_SECRET,
                                      "allowed_updates": tgbot.ALLOWED_UPDATES})
                self.stdout.write(f"Webhook set to {url}")
            elif o["delete_webhook"]:
                tg.api("deleteWebhook", {})
                self.stdout.write("Webhook removed")
            elif o["info"]:
                i = tg.api("getWebhookInfo", {})
                self.stdout.write(f"url={i.get('url') or '(none)'} pending={i.get('pending_update_count')} "
                                  f"last_error={i.get('last_error_message', '-')}")
            else:
                self.poll(o["once"])
        except tg.TelegramError as e:
            raise CommandError(f"Telegram: {e}") from None

    def poll(self, once):
        tg.api("deleteWebhook", {})   # getUpdates does not work while a webhook is set
        self.stdout.write("Polling for updates. Ctrl-C to stop.")
        offset = None
        while True:
            payload = {"timeout": 0 if once else 30, "allowed_updates": tgbot.ALLOWED_UPDATES}
            if offset:
                payload["offset"] = offset
            try:
                updates = tg.api("getUpdates", payload, timeout=10) or []
            except tg.TelegramError as e:
                self.stderr.write(f"getUpdates failed: {e}")
                if once:
                    return
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                tgbot.handle_update(u)
            if once:
                return
