"""
Shared viewset mixins for offline-writable models. See docs/05-offline-sync.md
and docs/04-api-spec.md § Conventions: "resubmitting the same UUID is
idempotent, not an error."
"""
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response


class IdempotentUUIDCreateMixin:
    """Overrides create() so that POSTing a record with a client-generated
    UUID that already exists updates it in place instead of raising an
    IntegrityError. This is what makes the offline sync queue safe to
    replay — a queued write that partially succeeded and gets retried
    after reconnecting won't produce a duplicate-key error or a duplicate
    row.

    Deliberately NOT a general-purpose upsert: this only makes sense for
    offline-writable models where the client is the source of truth for
    the ID (see CLAUDE.md — offline records use client-generated UUIDs).
    Server-only models should keep DRF's default create() behavior.

    Subclasses that need request-derived defaults on create (e.g. setting
    created_by/updated_by to the requesting user, the way a normal
    perform_create() override would) should implement
    get_idempotent_create_defaults(request) rather than relying on
    perform_create() — this path does not call perform_create(), since
    update_or_create() needs the extra fields up front, not after the fact.

    Scope enforcement on resubmit: if the client-supplied UUID already
    belongs to a row outside this user's scoped queryset (get_queryset()),
    the update is refused with 403 rather than silently applied against the
    unscoped manager. Without this check, any authenticated user who knew
    (or replayed/guessed) another user's record UUID could overwrite a row
    they have no farm/block access to — the scoping every list/detail
    endpoint enforces would be meaningless on this write path. This does
    NOT validate that a brand-new record's foreign keys (e.g. Tree.farm_block)
    are themselves in-scope — that's a separate, broader gap tracked in
    docs/13-open-questions.md, affecting the plain create() path too, not
    specific to this mixin.
    """

    def get_idempotent_create_defaults(self, request):
        return {}

    def is_resubmit_locked(self, instance):
        """Return True when an existing record must no longer change through
        a resubmit. A locked resubmit gets the stored record back unchanged
        (200) rather than an error, so a genuine offline replay still clears
        from the device's outbox, but a replay can't be used as a second
        edit path around ManagerOnlyChangesMixin (e.g. rewriting an
        already-approved harvest weight by re-POSTing its UUID).
        """
        return False

    def validate_new_record(self, validated_data):
        """Hook: raise ValidationError to refuse creating a brand-new record.
        Never called for a resubmit of a record that already exists, so a
        rule like "no new actuals on an approved plan" doesn't also reject
        a genuine offline replay of an actual created before approval.
        """

    def on_record_created(self, instance):
        """Hook: side effects of creating a brand-new record (not called on
        a resubmit). Runs in the same transaction as the create.
        """

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Use get_queryset() rather than the class-level `queryset` attribute —
        # several viewsets (e.g. HarvestTreeViewSet) only define get_queryset()
        # and leave `queryset` unset (None), which would crash here. get_queryset()
        # always returns a real queryset, so `.model` is always safe to access
        # regardless of which pattern a given viewset uses. It also doubles as
        # the scope check below.
        scoped_qs = self.get_queryset()
        model = scoped_qs.model
        pk_field = model._meta.pk.name
        pk_value = serializer.validated_data.get(pk_field)

        if pk_value is not None:
            exists_anywhere = model.objects.filter(**{pk_field: pk_value}).exists()
            exists_in_scope = scoped_qs.filter(**{pk_field: pk_value}).exists()
            if exists_anywhere and not exists_in_scope:
                raise PermissionDenied(
                    "A record with this id exists but is outside your scope; it cannot be updated via sync."
                )
            existing = scoped_qs.filter(**{pk_field: pk_value}).first() if exists_in_scope else None
            if existing is not None and self.is_resubmit_locked(existing):
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

            if existing is None:
                self.validate_new_record(serializer.validated_data)

            defaults = {**serializer.validated_data, **self.get_idempotent_create_defaults(request)}
            defaults.pop(pk_field, None)
            with transaction.atomic():
                instance, created = model.objects.update_or_create(
                    **{pk_field: pk_value}, defaults=defaults
                )
                if created:
                    self.on_record_created(instance)
            output = self.get_serializer(instance)
            return Response(
                output.data,
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )

        self.validate_new_record(serializer.validated_data)
        with transaction.atomic():
            self.perform_create(serializer)
            self.on_record_created(serializer.instance)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
