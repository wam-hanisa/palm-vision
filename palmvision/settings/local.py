"""
Local development settings — this is what DJANGO_SETTINGS_MODULE points to
by default (see manage.py and .env.example). Never used for a deployed
environment; see docs/11-local-development.md.
"""
from .base import *  # noqa: F401,F403

DEBUG = True

if not SECRET_KEY:
    # Only acceptable because this is the *local* settings module — never do
    # this in a production settings module.
    SECRET_KEY = "django-insecure-local-dev-only-change-me"

if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# django-debug-toolbar, extra logging, etc. can be added here as the project
# grows — keep base.py free of anything that shouldn't run in every environment.

CORS_ALLOW_ALL_ORIGINS = True  # local dev only — the PWA may be served from a different port/origin

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
