"""Handles incoming Telegram updates (from the webhook or getUpdates polling): deep-link linking and button presses.
The chat id is stored only to send messages; it is never logged, shown, exported or placed in an audit row."""
import logging
import uuid

from django.db import transaction
from django.utils import timezone

from . import escalation, notify, telegram as tg
from .models import CheckIn, EmergencyContact, Household, Notification, Profile, TelegramLink

log = logging.getLogger("watch.tgbot")
ALLOWED_UPDATES = ["message", "callback_query"]


def deep_link(link) -> str:
    from django.conf import settings
    bot = settings.TELEGRAM_BOT_USERNAME.lstrip("@")
    return f"https://t.me/{bot}?start={link.token}" if bot else ""


def new_link(user=None, contact=None, household=None):
    TelegramLink.objects.filter(user=user, contact=contact, household=household, used_at=None).delete()  # one live link at a time
    return TelegramLink.objects.create(user=user, contact=contact, household=household)


def is_linked(user=None, contact=None, household=None) -> bool:
    if user:
        return bool(Profile.objects.filter(user=user).exclude(telegram_chat_id=None).exists())
    if contact:
        return bool(EmergencyContact.objects.filter(pk=contact.pk).exclude(telegram_chat_id=None).exists())
    return bool(Household.objects.filter(pk=household.pk).exclude(person_telegram_chat_id=None).exists())


def bind(token: str, chat_id: int) -> str:
    """Bind chat_id to whoever the one-time token was issued for. Returns the name to greet, or '' if invalid."""
    with transaction.atomic():
        link = TelegramLink.objects.select_for_update().filter(token=token).first()
        if not link or not link.usable:
            return ""
        link.used_at = timezone.now()
        link.save()
        if link.user_id:
            p, _ = Profile.objects.get_or_create(user=link.user)
            p.telegram_chat_id = chat_id
            p.save()
            return link.user.get_short_name() or link.user.username
        if link.contact_id:
            EmergencyContact.objects.filter(pk=link.contact_id).update(telegram_chat_id=chat_id)
            return link.contact.name
        Household.objects.filter(pk=link.household_id).update(person_telegram_chat_id=chat_id)
        return link.household.name


def _reply(chat_id, text):
    try:
        tg.send_message(chat_id, text)
    except tg.TelegramError as e:
        log.warning("telegram reply failed", extra={"code": e.code})


def _on_message(msg):
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if chat.get("type") != "private" or not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    cid = chat.get("id")
    if cmd == "/start":
        arg = arg.strip()
        if not arg:
            return _reply(cid, "Hello. To get Quiet Morning messages here, open the link from the app (Settings, then Connect Telegram).")
        who = bind(arg[:64], cid)
        if who:
            _reply(cid, f"Connected. {who}, you will get Quiet Morning check-ins here, with All OK / Need help buttons.")
        else:
            _reply(cid, "That link has expired or was already used. Please make a new one in the app.")
    elif cmd == "/stop":
        n = (Profile.objects.filter(telegram_chat_id=cid).update(telegram_chat_id=None)
             + EmergencyContact.objects.filter(telegram_chat_id=cid).update(telegram_chat_id=None)
             + Household.objects.filter(person_telegram_chat_id=cid).update(person_telegram_chat_id=None))
        _reply(cid, "Disconnected. You will not get messages here." if n else "This chat was not connected.")


def _result_text(c, who, resolution):
    h = c.household.name
    return (f"\U0001F198 {who} asked for help for {h}. Everyone remaining is being alerted." if resolution == "needs_help"
            else f"✅ {who} confirmed {h} is OK. Thank you.")


def _on_callback(cq):
    qid, data = cq.get("id"), cq.get("data") or ""
    sender = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}

    def done(text, alert=False):
        try:
            tg.answer_callback(qid, text, alert)
        except tg.TelegramError as e:
            log.warning("answerCallbackQuery failed", extra={"code": e.code})

    def edit(text):
        if msg.get("message_id") and (msg.get("chat") or {}).get("id"):
            try:
                tg.edit_message(msg["chat"]["id"], msg["message_id"], text)
            except tg.TelegramError as e:   # e.g. "message is not modified" on a repeated tap
                log.info("editMessageText skipped", extra={"code": e.code})

    action, _, raw = data.partition(":")
    try:
        token = uuid.UUID(raw)
    except ValueError:
        return done("This button is no longer valid.")
    if action not in ("ok", "help"):
        return done("This button is no longer valid.")
    n = Notification.objects.select_related("checkin__household").filter(ack_token=token, channel="telegram").first()
    # The button must be pressed from the chat the message was sent to.
    if not n or sender is None or notify.target_for(n).chat_id != sender:
        return done("Sorry, this button is not for you.", True)
    c = n.checkin
    resolution = "needs_help" if action == "help" else ("person_ok" if n.step_kind == "nudge" else "all_ok")
    if c.state == CheckIn.HELP or (c.state == CheckIn.ACK and resolution != "needs_help"):
        # Idempotent: a second tap (or a tap after someone else acted) changes nothing.
        by = c.acknowledged_by or "Someone"
        done("Already recorded.")
        return edit(_result_text(c, by, c.resolution or resolution))
    escalation.acknowledge_by_token(token, resolution)
    c.refresh_from_db()
    done("Recorded. Thank you." if resolution != "needs_help" else "Help requested. Alerting the others.")
    edit(_result_text(c, n.recipient, resolution))


def handle_update(update: dict):
    """Never raises: Telegram retries a failing webhook, which would replay button presses."""
    try:
        if update.get("callback_query"):
            _on_callback(update["callback_query"])
        elif update.get("message"):
            _on_message(update["message"])
    except Exception:
        log.exception("telegram update failed")
