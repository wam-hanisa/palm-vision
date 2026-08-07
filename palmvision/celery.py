import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "palmvision.settings.local")

app = Celery("palmvision")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
