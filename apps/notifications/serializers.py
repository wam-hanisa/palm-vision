from rest_framework import serializers

from .models import Notification, NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "category", "title", "message", "link", "read", "created_at"]
        read_only_fields = ["id", "category", "title", "message", "link", "created_at"]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = ["id", "category", "in_app_enabled", "email_enabled"]
        read_only_fields = ["id"]
