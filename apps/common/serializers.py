"""
Serializer-side scoping for foreign-key writes — closes the gap noted in
BUILD_STATUS.md's "Known gaps": DRF's PrimaryKeyRelatedField only checks
that a referenced row *exists*, not that the requesting user is allowed to
reference it. Without this, a PIC Block user could POST a Tree naming a
farm_block they have no access to (or a HarvestTree naming another farm's
tree), and it would succeed — the read-side scoping every get_queryset()
already enforces was meaningless on the write path.

The fix: for the FK fields named in a serializer's `scoped_pk_fields`,
narrow that PrimaryKeyRelatedField's `.queryset` to exactly what the
requesting user is scoped to see (via apps/common/scoping.py — the same
functions the read-side get_queryset()s use). DRF's own
PrimaryKeyRelatedField.to_internal_value() already raises a standard
"object does not exist" 400 for a pk not in its queryset — narrowing the
queryset makes an out-of-scope reference indistinguishable from one that
doesn't exist at all, from the client's perspective. That's deliberate,
consistent with this project's existing pattern of 404ing rather than
leaking the existence of records outside a user's scope (see get_object()
everywhere else in this codebase) — an out-of-scope FK reference gets a
plain "invalid pk" validation error, not a permission-denied response that
would confirm the referenced row exists somewhere the user can't see.

This is scoped per-request (in get_fields(), not at class-definition time)
since "what's in scope" always depends on who's asking. It applies equally
whichever create path a serializer goes through — the plain DRF
ModelViewSet.create() and apps/common/mixins.py's IdempotentUUIDCreateMixin
both call serializer.is_valid() before doing anything else, and this hooks
field construction, which runs during is_valid() either way.
"""


class ScopedPrimaryKeyFieldsMixin:
    """Serializer mixin. Subclasses set `scoped_pk_fields`, a dict mapping
    field name -> callable(user) -> queryset (see apps/common/scoping.py
    for the callables actually used).

    No-ops (leaves fields with their default, unscoped queryset) when
    there's no request in context or the user isn't authenticated — this
    only ever runs behind permission_classes=[IsAuthenticated] in practice,
    but stays defensive rather than assuming that's always true (e.g.
    schema generation, or a serializer instantiated outside a view).
    """

    scoped_pk_fields = {}

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return fields
        for field_name, scope_fn in self.scoped_pk_fields.items():
            field = fields.get(field_name)
            if field is not None and hasattr(field, "queryset"):
                field.queryset = scope_fn(user)
        return fields
