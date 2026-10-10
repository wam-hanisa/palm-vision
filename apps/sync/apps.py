from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sync"
    label = "sync"

    def ready(self):
        from . import signals  # noqa: F401 — registers this app's pre_delete tombstone receivers
