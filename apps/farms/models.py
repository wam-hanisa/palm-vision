import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords


class Farm(models.Model):
    """Top-level container — see docs/03-data-model.md."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class FarmBlock(gis_models.Model):
    """A plantation plot within a Farm. `boundary` is a real PostGIS polygon —
    never a text/string field, per docs/01-architecture.md's "no text-string
    coordinates anywhere" rule.

    `pic` is the single field this whole app's block-level PIC Block
    permission checks hinge on (apps/common/permissions.py) — mirrors the
    original Laravel app's farm_blocks.user_id assignment (Palm_Vision_PDR.md).

    `tree_match_threshold_m`: per-block override for the tree-detection
    matching distance threshold (docs/06-ml-pipeline.md). Falls back to
    settings.TREE_MATCH_DEFAULT_THRESHOLD_M when null — don't hardcode a
    global constant in code that uses this field.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="blocks")
    name = models.CharField(max_length=255)
    boundary = gis_models.PolygonField(
        srid=4326,
        null=True,
        blank=True,
        help_text=(
            "Nullable: a block can exist as an administrative record before its "
            "boundary is surveyed/drawn. docs/06-ml-pipeline.md requires imagery "
            "processing to refuse to run against a block with no boundary — see "
            "apps/imagery/services.py run_processing() — which is only a real "
            "precondition, not dead code, because this field can actually be None."
        ),
    )
    commodity = models.CharField(max_length=255, blank=True)
    area = models.FloatField(null=True, blank=True, help_text="Hectares")
    year = models.PositiveIntegerField(null=True, blank=True, help_text="Planting year")
    pic = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pic_blocks",
        help_text="The PIC Block user assigned to this specific block.",
    )
    tree_match_threshold_m = models.FloatField(
        null=True,
        blank=True,
        help_text="Overrides TREE_MATCH_DEFAULT_THRESHOLD_M for this block. See docs/06-ml-pipeline.md.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["farm", "name"]

    def __str__(self):
        return f"{self.farm.name} / {self.name}"

    def clean(self):
        """Geometry validity check — docs/12-security-ops.md § Geometry &
        Data Integrity Validation: 'FarmBlock.boundary is checked for
        geometric validity (no self-intersection) at save time via GEOS's
        is_valid; an invalid polygon is rejected with a clear error.'
        """
        super().clean()
        if self.boundary and not self.boundary.valid:
            raise ValidationError(
                {"boundary": f"This polygon is not geometrically valid ({self.boundary.valid_reason})."}
            )

    def save(self, *args, **kwargs):
        # Deliberately calling only the geometry check here, not full_clean() —
        # full_clean() would also enforce field-level required/blank rules at
        # the ORM layer, which is redundant with (and can conflict with) DRF
        # serializer validation used by the API. The boundary-validity check
        # is the one rule that must hold no matter which path writes this
        # model (admin, API, management command, shell) — see
        # docs/12-security-ops.md § Geometry & Data Integrity Validation.
        if self.boundary and not self.boundary.valid:
            raise ValidationError(
                {"boundary": f"This polygon is not geometrically valid ({self.boundary.valid_reason})."}
            )
        super().save(*args, **kwargs)

    def effective_tree_match_threshold_m(self):
        from django.conf import settings as django_settings

        return self.tree_match_threshold_m or django_settings.TREE_MATCH_DEFAULT_THRESHOLD_M

    def overlapping_blocks(self, tolerance_ratio=0.01):
        """Returns other blocks on the same farm whose boundary overlaps
        this one's by more than a small tolerance. This is a WARNING check,
        not enforced at save time — docs/12-security-ops.md is explicit that
        adjacent blocks can legitimately share a boundary edge, so this is
        surfaced to the caller (e.g. a serializer) to show as a non-blocking
        warning, not raised as a ValidationError here.
        """
        if not self.boundary:
            return []  # nothing to compare against yet — see boundary's nullability note above
        others = FarmBlock.objects.filter(farm_id=self.farm_id, boundary__isnull=False).exclude(pk=self.pk)
        overlapping = []
        for other in others:
            if self.boundary.intersects(other.boundary):
                intersection = self.boundary.intersection(other.boundary)
                if intersection.area > (self.boundary.area * tolerance_ratio):
                    overlapping.append(other)
        return overlapping


class UserFarmRole(models.Model):
    """Farm-level role scoping — docs/12-security-ops.md § Multi-Farm Role
    Scoping. Super Admin is NOT represented here; it's Django's is_superuser,
    global by definition. This table only covers Manager and PIC Block.

    Block-level PIC Block assignment (which specific blocks a PIC Block user
    can act on) is FarmBlock.pic, not this table — see apps/common/permissions.py
    for how the two combine.
    """

    ROLE_MANAGER = "manager"
    ROLE_PIC_BLOCK = "pic_block"
    ROLE_CHOICES = [
        (ROLE_MANAGER, "Manager"),
        (ROLE_PIC_BLOCK, "PIC Block"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="farm_roles")
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="user_roles")
    role = models.CharField(max_length=32, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "farm", "role")

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} @ {self.farm}"
