"""
docs/02-build-plan.md Phase 6 verification gate: "upload a sample compliance
document with a near-future expiry date, confirm its status computes
correctly; generate a due-diligence export for a seeded HarvestBatch and
confirm it includes the block's polygon coordinates and a deforestation-
baseline status field."
"""
import datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status

from apps.compliance.models import (
    ChecklistItem,
    ComplianceAuditTemplate,
    ComplianceDocument,
    DeforestationBaseline,
    HarvestBatch,
)
from apps.operations.models import ActualHarvest, Harvest


def _today_plus(days):
    """ISO string — for API calls (multipart/JSON form fields are strings;
    DRF's DateField parses this into a real date during is_valid(), so the
    resulting model instance ends up with a proper `datetime.date`, same as
    every real client of this API).
    """
    return (timezone.localdate() + datetime.timedelta(days=days)).isoformat()


def _today_plus_date(days):
    """Real `datetime.date` — for constructing model instances directly via
    `.objects.create()`, bypassing DRF entirely. Assigning a raw string to a
    DateField and reading it back off the *same in-memory instance* (no
    save+refetch, no full_clean()) does not get coerced to `date` — that's
    plain Django model-field behavior, not specific to this project. Every
    real path that creates a ComplianceDocument (the API serializer, the
    admin) already hands the model a proper `date` object, so this isn't a
    gap in apps/compliance/models.py; it was a bug in this test file's
    original use of `_today_plus()` (a string) for direct `.create()` calls,
    caught by a real pytest run — fixed here, not in the model.
    """
    return timezone.localdate() + datetime.timedelta(days=days)


def _dummy_file(name="doc.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 fake content", content_type="application/pdf")


# --- ComplianceDocument.status computed property ---

@pytest.mark.django_db
def test_document_status_valid_when_expiry_far_in_future(block_a):
    doc = ComplianceDocument.objects.create(
        farm_block=block_a, document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=_dummy_file(), expiry_date=_today_plus_date(400),
    )
    assert doc.status == ComplianceDocument.STATUS_VALID


@pytest.mark.django_db
def test_document_status_expiring_soon_within_default_90_day_window(block_a):
    doc = ComplianceDocument.objects.create(
        farm_block=block_a, document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=_dummy_file(), expiry_date=_today_plus_date(45),
    )
    assert doc.status == ComplianceDocument.STATUS_EXPIRING_SOON


@pytest.mark.django_db
def test_document_status_expired_when_expiry_in_past(block_a):
    doc = ComplianceDocument.objects.create(
        farm_block=block_a, document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=_dummy_file(), expiry_date=_today_plus_date(-5),
    )
    assert doc.status == ComplianceDocument.STATUS_EXPIRED


@pytest.mark.django_db
def test_document_status_valid_when_no_expiry_at_all(block_a):
    doc = ComplianceDocument.objects.create(
        farm_block=block_a, document_type=ComplianceDocument.TYPE_LAND_TITLE, document_file=_dummy_file(),
    )
    assert doc.status == ComplianceDocument.STATUS_VALID


@pytest.mark.django_db
def test_document_must_attach_to_exactly_one_of_farm_or_block(farm, block_a):
    with pytest.raises(ValidationError):
        ComplianceDocument.objects.create(
            farm=farm, farm_block=block_a, document_type=ComplianceDocument.TYPE_OTHER, document_file=_dummy_file(),
        )
    with pytest.raises(ValidationError):
        ComplianceDocument.objects.create(
            document_type=ComplianceDocument.TYPE_OTHER, document_file=_dummy_file(),
        )


@pytest.mark.django_db
def test_manager_can_upload_document_via_api(as_manager, block_a):
    response = as_manager.post(
        "/api/compliance-documents/",
        {
            "farm_block": str(block_a.id),
            "document_type": ComplianceDocument.TYPE_ISPO_CERT,
            "document_file": _dummy_file(),
            "expiry_date": _today_plus(45),
        },
        format="multipart",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    assert response.data["status"] == ComplianceDocument.STATUS_EXPIRING_SOON


@pytest.mark.django_db
def test_pic_block_cannot_upload_document(as_pic_block, block_a):
    response = as_pic_block.post(
        "/api/compliance-documents/",
        {
            "farm_block": str(block_a.id),
            "document_type": ComplianceDocument.TYPE_ISPO_CERT,
            "document_file": _dummy_file(),
        },
        format="multipart",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


# --- DeforestationBaseline ---

@pytest.mark.django_db
def test_manager_can_create_and_verify_baseline(as_manager, block_a):
    create_response = as_manager.post(
        "/api/deforestation-baselines/",
        {"farm_block": str(block_a.id), "cutoff_date": "2020-12-31", "source": "Satellite provider X"},
    )
    assert create_response.status_code == status.HTTP_201_CREATED, create_response.data
    baseline_id = create_response.data["id"]

    verify_response = as_manager.post(
        f"/api/deforestation-baselines/{baseline_id}/verify/",
        {"status": DeforestationBaseline.STATUS_DEFORESTATION_FREE},
    )
    assert verify_response.status_code == status.HTTP_200_OK
    baseline = DeforestationBaseline.objects.get(id=baseline_id)
    assert baseline.status == DeforestationBaseline.STATUS_DEFORESTATION_FREE
    assert baseline.verified_by_id is not None
    assert baseline.verified_at is not None


@pytest.mark.django_db
def test_pic_block_can_read_but_not_create_baseline(as_pic_block, block_a, block_b):
    baseline = DeforestationBaseline.objects.create(farm_block=block_a, source="Manual survey")
    list_response = as_pic_block.get("/api/deforestation-baselines/")
    assert list_response.status_code == status.HTTP_200_OK
    assert str(baseline.id) in [row["id"] for row in list_response.data["results"]]

    # block_b (not block_a) deliberately — block_a already has a baseline
    # above, and DeforestationBaseline.farm_block is a OneToOneField, so
    # reusing block_a here would 400 on the uniqueness constraint before the
    # permission check even runs, masking the thing this test is actually
    # checking. block_b has no baseline yet, isolating the assertion to
    # "PIC Block isn't a Manager, so create is forbidden" — a real pytest
    # run originally caught this test conflating the two failure modes.
    create_response = as_pic_block.post(
        "/api/deforestation-baselines/", {"farm_block": str(block_b.id), "source": "attempted"}
    )
    assert create_response.status_code == status.HTTP_403_FORBIDDEN


# --- HarvestBatch + due-diligence export ---

@pytest.fixture
def harvest_batch(db, block_a):
    return HarvestBatch.objects.create(
        farm_block=block_a, period_start="2026-07-01", period_end="2026-07-31",
    )


@pytest.fixture
def harvest_plan(db, block_a):
    return Harvest.objects.create(farm_block=block_a, assessment_date="2026-07-15", assessment_quantity=1000.0)


@pytest.mark.django_db
def test_recompute_totals_sums_linked_actual_harvests(harvest_batch, harvest_plan):
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-07-10", quantity=300.0, batch=harvest_batch)
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-07-20", quantity=450.0, batch=harvest_batch)
    total_qty, total_weight = harvest_batch.recompute_totals()
    assert total_qty == 750.0
    assert total_weight == 0.0


@pytest.mark.django_db
def test_export_due_diligence_includes_block_polygon_and_baseline_status(as_manager, harvest_batch, harvest_plan, block_a):
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-07-10", quantity=300.0, batch=harvest_batch)
    DeforestationBaseline.objects.create(
        farm_block=block_a, source="Satellite provider X", status=DeforestationBaseline.STATUS_DEFORESTATION_FREE,
    )

    response = as_manager.post(f"/api/harvest-batches/{harvest_batch.id}/export-due-diligence/")
    assert response.status_code == status.HTTP_200_OK, response.data
    data = response.data

    assert data["plot_geometry"]["type"] == "Feature"
    assert data["plot_geometry"]["geometry"]["type"] == "Polygon"
    coords = data["plot_geometry"]["geometry"]["coordinates"][0]
    assert len(coords) >= 4
    # 6-decimal precision formatting — docs/07-compliance.md.
    for lon, lat in coords:
        assert len(str(lon).split(".")[-1]) <= 6
        assert len(str(lat).split(".")[-1]) <= 6
    # block_a's fixture area is 25.0ha, >= the EUDR 4ha threshold.
    assert data["plot_geometry"]["properties"]["requires_geojson_polygon_per_eudr_4ha_rule"] is True

    assert data["deforestation_baseline"]["status"] == DeforestationBaseline.STATUS_DEFORESTATION_FREE
    assert data["total_quantity"] == 300.0

    harvest_batch.refresh_from_db()
    assert harvest_batch.export_status == HarvestBatch.EXPORT_STATUS_EXPORTED


@pytest.mark.django_db
def test_export_due_diligence_baseline_null_when_none_seeded(as_manager, harvest_batch):
    response = as_manager.post(f"/api/harvest-batches/{harvest_batch.id}/export-due-diligence/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["deforestation_baseline"] is None


@pytest.mark.django_db
def test_export_due_diligence_refused_when_block_has_no_boundary(as_manager, farm):
    from apps.farms.models import FarmBlock

    block_no_boundary = FarmBlock.objects.create(farm=farm, name="Unsurveyed block")
    batch = HarvestBatch.objects.create(
        farm_block=block_no_boundary, period_start="2026-07-01", period_end="2026-07-31",
    )
    response = as_manager.post(f"/api/harvest-batches/{batch.id}/export-due-diligence/")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_pic_block_cannot_trigger_due_diligence_export(as_pic_block, harvest_batch):
    """docs/02-build-plan.md Phase 8: "compliance export correctness" is
    listed as a highest-risk-alongside-approval coverage target — this was
    the one gap in the original Phase 6 test file: every export test used
    as_manager, so nothing actually asserted a non-Manager is refused.
    """
    response = as_pic_block.post(f"/api/harvest-batches/{harvest_batch.id}/export-due-diligence/")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    harvest_batch.refresh_from_db()
    assert harvest_batch.export_status == HarvestBatch.EXPORT_STATUS_DRAFT  # unchanged


@pytest.mark.django_db
def test_pic_block_cannot_create_harvest_batch(as_pic_block, block_a):
    response = as_pic_block.post(
        "/api/harvest-batches/",
        {"farm_block": str(block_a.id), "period_start": "2026-07-01", "period_end": "2026-07-31"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


# --- ComplianceAudit + checklist ---

@pytest.mark.django_db
def test_audit_result_scoped_to_farm_block_via_audit(as_manager, as_other_pic_block, block_a):
    template = ComplianceAuditTemplate.objects.create(name="ISPO checklist", certification_scheme="ISPO")
    ChecklistItem.objects.create(template=template, text="No burning practices observed", order=1)

    audit_response = as_manager.post(
        "/api/compliance-audits/",
        {
            "farm_block": str(block_a.id),
            "template": str(template.id),
            "audit_date": "2026-08-01",
            "auditor_name": "Jane Auditor",
            "auditor_type": "third_party",
            "overall_status": "passed",
        },
    )
    assert audit_response.status_code == status.HTTP_201_CREATED, audit_response.data

    # A PIC Block user with no assignment on this farm shouldn't see the audit.
    other_list = as_other_pic_block.get("/api/compliance-audits/")
    ids = [row["id"] for row in other_list.data["results"]]
    assert audit_response.data["id"] not in ids


# --- GrievanceRecord ---

@pytest.mark.django_db
def test_manager_can_log_grievance_pic_block_cannot(as_manager, as_pic_block, farm):
    response = as_manager.post(
        "/api/grievances/",
        {"farm": str(farm.id), "date_reported": "2026-08-01", "description": "Boundary dispute reported by neighbor."},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data

    forbidden = as_pic_block.post(
        "/api/grievances/",
        {"farm": str(farm.id), "date_reported": "2026-08-01", "description": "attempted"},
    )
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN


# --- Image storage-lifecycle exemption hook ---

@pytest.mark.django_db
def test_image_referenced_by_baseline_is_compliance_exempt(block_a, farm):
    from apps.imagery.models import Image

    image = Image.objects.create(farm=farm, raw_file="imagery/raw/does-not-exist.tif")
    assert image.is_compliance_exempt() is False

    DeforestationBaseline.objects.create(farm_block=block_a, source="drone survey", evidence_image=image)
    assert image.is_compliance_exempt() is True
