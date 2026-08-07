"""
Base settings shared by every environment. Do not put environment-specific
values (DEBUG=True, local DB creds, etc.) here — those go in local.py, and
a future production.py. See docs/01-architecture.md and docs/11-local-development.md
in the doc-set this project was built from.
"""
import os
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "")
DEBUG = os.environ.get("DEBUG", "False") == "True"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",") if os.environ.get("ALLOWED_HOSTS") else []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",  # GeoDjango — required for every PointField/PolygonField in this project
    # Third-party
    "rest_framework",
    "rest_framework_gis",
    "rest_framework_simplejwt",
    "corsheaders",
    "simple_history",
    # Project apps (add new ones here as later build phases introduce them —
    # see docs/02-build-plan.md for the phase each app belongs to)
    "apps.users",
    "apps.farms",
    "apps.trees",
    "apps.operations",
    "apps.sync",
    "apps.imagery",
    "apps.compliance",
    "apps.reporting",
    "apps.notifications",
    # Phase 8: polish & pre-deployment hardening — no new apps expected
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",  # captures "current user" for audit logging — docs/12-security-ops.md
]

ROOT_URLCONF = "palmvision.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "palmvision.wsgi.application"
ASGI_APPLICATION = "palmvision.asgi.application"

AUTH_USER_MODEL = "users.User"

# Only needed if GDAL/GEOS aren't discoverable via the system's normal
# library search path — the Dockerfile installs them as proper system
# packages (gdal-bin/libgdal-dev, libgeos-dev), so these should stay unset
# in the container. Left as an escape hatch for unusual local setups.
if os.environ.get("GDAL_LIBRARY_PATH"):
    GDAL_LIBRARY_PATH = os.environ["GDAL_LIBRARY_PATH"]
if os.environ.get("GEOS_LIBRARY_PATH"):
    GEOS_LIBRARY_PATH = os.environ["GEOS_LIBRARY_PATH"]

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("DB_NAME", "palmvision"),
        "USER": os.environ.get("DB_USER", "palmvision"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "palmvision"),
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Jakarta"  # Indonesian plantation operation — see Palm_Vision_PDR.md for context
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Media (uploaded imagery — docs/06-ml-pipeline.md) ---
# Local filesystem storage for local dev. django-storages (already in
# requirements.txt) + real AWS_* env vars switch this to S3-compatible
# storage for a real deployment — not configured here, since that's
# explicitly a deployment concern (CLAUDE.md's local-first rule), and
# apps/imagery/services.py's georeference_image()/clip_to_block() currently
# assume a local filesystem path (`raw_file.path`) regardless — see that
# file's docstring for the known gap this leaves for cloud storage.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- REST Framework ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 60))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.environ.get("JWT_REFRESH_TOKEN_LIFETIME_DAYS", 14))),
}

# --- Celery (docs/06-ml-pipeline.md, docs/08-reporting.md, docs/12-security-ops.md) ---
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# Plain in-code schedule (Celery's built-in beat scheduler, not
# django-celery-beat) — docs/02-build-plan.md Phase 7 doesn't ask for
# admin-editable schedules, just periodic execution, which this gives
# without an extra dependency + extra migrations for a DB-backed
# scheduler. All six checks run daily; docs/12-security-ops.md itself
# describes them as "daily-digest material" except the failed-processing-run
# trigger, which is event-driven instead — see
# apps/imagery/services.py's _notify_processing_failure(), not this
# schedule. run_scheduled_reports (apps/reporting/tasks.py) also runs
# daily and decides internally which ReportDefinitions are actually due
# today (see that task's docstring).
CELERY_BEAT_SCHEDULE = {
    "check-compliance-expiry": {
        "task": "apps.notifications.tasks.check_compliance_expiry",
        "schedule": crontab(hour=6, minute=0),
    },
    "check-tree-health-anomalies": {
        "task": "apps.notifications.tasks.check_tree_health_anomalies",
        "schedule": crontab(hour=6, minute=15),
    },
    "check-stalled-sync": {
        "task": "apps.notifications.tasks.check_stalled_sync",
        "schedule": crontab(hour=6, minute=30),
    },
    "check-review-backlog": {
        "task": "apps.notifications.tasks.check_review_backlog",
        "schedule": crontab(hour=6, minute=45),
    },
    "check-approvals-pending": {
        "task": "apps.notifications.tasks.check_approvals_pending",
        "schedule": crontab(hour=7, minute=0),
    },
    "run-scheduled-reports": {
        "task": "apps.reporting.tasks.run_scheduled_reports",
        "schedule": crontab(hour=5, minute=0),
    },
}

# --- Email (docs/12-security-ops.md § Alerting & Notifications) ---
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@palmvision.local")

# --- Notification thresholds (docs/12-security-ops.md § Alerting & notifications) ---
# 90/60/30-day escalating windows for compliance document expiry — exact-day
# matches, not "less than N days," so each document notifies three times,
# not once a day for the whole window. See apps/notifications/tasks.py.
COMPLIANCE_NOTIFICATION_THRESHOLDS_DAYS = [
    int(d) for d in os.environ.get("COMPLIANCE_NOTIFICATION_THRESHOLDS_DAYS", "90,60,30").split(",")
]
TREE_HEALTH_ANOMALY_WINDOW_DAYS = int(os.environ.get("TREE_HEALTH_ANOMALY_WINDOW_DAYS", 7))
TREE_HEALTH_ANOMALY_MIN_INCREASE = int(os.environ.get("TREE_HEALTH_ANOMALY_MIN_INCREASE", 5))
STALLED_SYNC_DAYS = int(os.environ.get("STALLED_SYNC_DAYS", 3))
REVIEW_BACKLOG_THRESHOLD = int(os.environ.get("REVIEW_BACKLOG_THRESHOLD", 20))
APPROVAL_PENDING_DAYS = int(os.environ.get("APPROVAL_PENDING_DAYS", 3))

# --- Domain-specific config ---
# Fallback tree-matching distance threshold in meters, used when a FarmBlock
# doesn't set its own tree_match_threshold_m. See docs/06-ml-pipeline.md.
TREE_MATCH_DEFAULT_THRESHOLD_M = float(os.environ.get("TREE_MATCH_DEFAULT_THRESHOLD_M", 3.0))

# Paths to trained model weights — apps/imagery/ml/detection.py and
# classification.py fall back to a stub (no detections / always-low-
# confidence) when these are unset, which is the expected state until real
# labeled data and trained weights exist. See docs/06-ml-pipeline.md
# § Labeling data — the real bottleneck, not the model.
DETECTION_MODEL_PATH = os.environ.get("DETECTION_MODEL_PATH", "")
CLASSIFICATION_MODEL_PATH = os.environ.get("CLASSIFICATION_MODEL_PATH", "")

# Days before ComplianceDocument.expiry_date that a document's computed
# status flips from 'valid' to 'expiring_soon'. docs/12-security-ops.md's
# alerting section mentions 90/60/30-day windows for escalating reminders;
# this is the single threshold apps/compliance/models.py's status property
# currently uses. Phase 7 (notifications) can layer the 60/30-day escalating
# reminders on top of this without changing the underlying computed status.
COMPLIANCE_EXPIRY_WARNING_DAYS = int(os.environ.get("COMPLIANCE_EXPIRY_WARNING_DAYS", 90))
