"""Quiet Morning settings. Everything deployment-specific comes from the environment (see .env.example)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.getenv(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
if not DEBUG and SECRET_KEY == "dev-only-change-me":
    raise RuntimeError("Set DJANGO_SECRET_KEY when DJANGO_DEBUG is off")
ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "watch",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")   # serve collected static files in production
ROOT_URLCONF = "quietmorning.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "watch.context.ui"]},
}]
WSGI_APPLICATION = "quietmorning.wsgi.application"
def _database():
    """SQLite by default. DATABASE_URL=postgres://user:pass@host:5432/name switches to Postgres (needs psycopg,
    see requirements-aws.txt). Postgres is recommended for real use: escalation uses SELECT ... FOR UPDATE."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": os.getenv("DB_PATH", BASE_DIR / "db.sqlite3")}
    from urllib.parse import unquote, urlparse
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must be postgres://...")
    return {"ENGINE": "django.db.backends.postgresql", "NAME": u.path.lstrip("/"), "USER": unquote(u.username or ""),
            "PASSWORD": unquote(u.password or ""), "HOST": u.hostname or "", "PORT": u.port or "",
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60"))}


DATABASES = {"default": _database()}
AUTH_PASSWORD_VALIDATORS = [{"NAME": f"django.contrib.auth.password_validation.{n}"} for n in
                            ("UserAttributeSimilarityValidator", "MinimumLengthValidator",
                             "CommonPasswordValidator", "NumericPasswordValidator")]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Africa/Lagos")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024   # webhook bodies are tiny; refuse anything big

if not DEBUG:
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))      # enable once HTTPS is confirmed end to end
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT")                # leave off if the load balancer health-checks over HTTP

# --- Celery ---
CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# --- Ring ---
RING_WEBHOOK_SECRET = os.getenv("RING_WEBHOOK_SECRET", "dev-secret")
# Reject events whose created_at is older than this (0 disables replay protection).
RING_REPLAY_WINDOW_SECONDS = int(os.getenv("RING_REPLAY_WINDOW_SECONDS", "900"))

# --- Notifications ---
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "quietmorning@localhost")
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
if os.getenv("EMAIL_HOST"):
    EMAIL_HOST = os.getenv("EMAIL_HOST")
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
# Telegram Bot API. Without a token, messages are logged stubs. Never commit the token.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")          # without @; used for t.me deep links
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")      # 1-256 chars of A-Z a-z 0-9 _ - ; empty = webhook rejects all
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")     # base64url application server key
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM = os.getenv("VAPID_CLAIM", "mailto:admin@example.com")

# --- Logging: one JSON object per line ---
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"json": {"()": "watch.logging_utils.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "loggers": {"watch": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO"), "propagate": False}},
}

import sys  # noqa: E402
if "test" in sys.argv:
    LOGGING["loggers"]["watch"]["level"] = "CRITICAL"
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]   # tests only: PBKDF2 makes each setUp seconds long

# ---- Optional AWS pieces. Everything defaults to OFF; the deterministic product works with none of it. ----
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
# Bedrock Converse. BEDROCK_MODEL_ID is deliberately empty by default: pick an ID your account can call
# (bare model ID or an inference-profile ID, see docs/AWS_STACK.md). Empty = feature off.
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")
BEDROCK_WORDING = os.getenv("BEDROCK_WORDING", "0").lower() in ("1", "true", "yes", "on")   # gentle wording + weekly narrative
BEDROCK_GUARDRAIL_ID = os.getenv("BEDROCK_GUARDRAIL_ID", "")
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION", "")
BEDROCK_DAILY_CALL_CAP = int(os.getenv("BEDROCK_DAILY_CALL_CAP", "200"))          # hard cap on model calls per day
BEDROCK_MAX_OUTPUT_TOKENS = int(os.getenv("BEDROCK_MAX_OUTPUT_TOKENS", "200"))
BEDROCK_TIMEOUT_SECONDS = int(os.getenv("BEDROCK_TIMEOUT_SECONDS", "8"))
# Care-coordinator advisor: off | bedrock (Converse) | strands (Strands Agents SDK). Advisory only, see watch/aws/advisor.py.
AI_ADVISOR = os.getenv("AI_ADVISOR", "off").lower()
# SQS worker (replaces Celery + Redis when running the AWS-native path)
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
