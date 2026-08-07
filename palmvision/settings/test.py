"""
Settings for running the automated test suite (pytest-django).

Same Postgres/PostGIS backend as local.py — GeoDjango model fields
(PointField/PolygonField) need real PostGIS, not SQLite, so tests run
against the same `db` service from docker-compose.yml. Point
`DB_HOST`/`DJANGO_SETTINGS_MODULE=palmvision.settings.test` at whatever
Postgres+PostGIS instance is available; see docs/11-local-development.md.
"""
from .base import *  # noqa: F401,F403

DEBUG = False

# Fast password hashing — tests create many users and don't need real
# hashing cost. Never used outside DJANGO_SETTINGS_MODULE=...settings.test.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Run Celery tasks synchronously/eagerly in tests instead of requiring a
# live Redis broker — see docs/06-ml-pipeline.md for the tasks this affects
# once Phase 5 lands.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Separate media directory for test-uploaded imagery (apps/imagery) so test
# runs don't write into whatever real MEDIA_ROOT local dev is using.
MEDIA_ROOT = BASE_DIR / "test_media"  # noqa: F405 — BASE_DIR comes from base.py via the star import above
