from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Palm Vision profile", {"fields": ("nik", "phone")}),
    )
    list_display = ("username", "get_full_name", "email", "is_staff", "is_superuser")
