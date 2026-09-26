"""Minimal Telegram Bot API client (https://core.telegram.org/bots/api).

Requests go to https://api.telegram.org/bot<TOKEN>/<method>. The token is a secret: it is never logged, stored, or
left in an error message (requests embeds the URL in exceptions, so every error is scrubbed).
Without a token, nothing is sent: callers get a "stubbed" result. Tests and the demo can install a fake transport."""
import html
import logging

from django.conf import settings

log = logging.getLogger("watch.telegram")
API = "https://api.telegram.org"
MAX_TEXT = 4096                # sendMessage text limit, counted after entity parsing
CALLBACK_MAX_BYTES = 64        # InlineKeyboardButton.callback_data limit
_transport = None              # optional fake: fn(method, payload) -> result dict; see set_transport()


class TelegramError(Exception):
    def __init__(self, message, code=0, retry_after=0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


def set_transport(fn):
    """Install (or clear with None) a fake transport. Used by tests and the offline demo."""
    global _transport
    _transport = fn


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN or _transport)


def escape(text: str) -> str:
    """HTML parse mode: only &, < and > need escaping in message text."""
    return html.escape(str(text), quote=False)


def html_text(raw: str, limit=MAX_TEXT) -> str:
    """Escape for HTML parse mode and cap at the limit, cutting the RAW text (never mid-entity)."""
    out, size = [], 0
    for i, ch in enumerate(str(raw)):
        piece = escape(ch)
        if size + len(piece) > limit - 1 and i < len(raw) - 1:
            out.append("\u2026")
            break
        out.append(piece)
        size += len(piece)
    return "".join(out)


def _scrub(msg: str) -> str:
    tok = settings.TELEGRAM_BOT_TOKEN
    return str(msg).replace(tok, "<token>") if tok else str(msg)


def api(method: str, payload: dict | None = None, timeout=10):
    payload = payload or {}
    if _transport:
        return _transport(method, payload)
    if not settings.TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN not set")
    import requests
    try:
        r = requests.post(f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", json=payload, timeout=timeout + payload.get("timeout", 0))
    except requests.RequestException as e:
        raise TelegramError(f"network error: {type(e).__name__}") from None   # the exception text contains the URL (token)
    try:
        body = r.json()
    except ValueError:
        raise TelegramError(f"non-JSON response (HTTP {r.status_code})", r.status_code) from None
    if not body.get("ok"):
        raise TelegramError(_scrub(body.get("description", "error"))[:150], body.get("error_code", r.status_code),
                            (body.get("parameters") or {}).get("retry_after", 0))
    return body.get("result")


def keyboard(ack_token) -> dict:
    t = str(ack_token).replace("-", "")
    return {"inline_keyboard": [[{"text": "✅ All OK", "callback_data": f"ok:{t}"},
                                 {"text": "\U0001F198 Need help", "callback_data": f"help:{t}"}]]}


def send_message(chat_id, raw_text, reply_markup=None):
    p = {"chat_id": chat_id, "text": html_text(raw_text), "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return api("sendMessage", p)


def edit_message(chat_id, message_id, raw_text):
    return api("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": html_text(raw_text),
                                   "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []}})


def answer_callback(callback_query_id, text="", alert=False):
    return api("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:200], "show_alert": alert})
