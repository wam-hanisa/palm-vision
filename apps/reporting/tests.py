"""
docs/02-build-plan.md Phase 7 verification gate (report half): "trigger a
report generation on-demand and confirm a downloadable file is produced."

CELERY_TASK_ALWAYS_EAGER=True in palmvision/settings/test.py means
generate_report_task.delay(...) below actually runs synchronously inline —
by the time the API response comes back, the ReportRun really is READY (or
FAILED with a real reason), not left pending for a worker that doesn't
exist in the test process. Same pattern apps/imagery/tests.py already
relies on for run_processing_task.
"""
import pytest
from rest_framework import status

from apps.farms.models import Farm
from apps.operations.models import ActualHarvest, Harvest
from apps.reporting.models import ReportDefinition, ReportRun
from apps.reporting.services import generate_report


@pytest.fixture
def harvest_data(db, block_a):
    plan = Harvest.objects.create(farm_block=block_a, assessment_date="2026-07-15", assessment_quantity=1000.0)
    ActualHarvest.objects.create(harvest=plan, date="2026-07-20", quantity=950.0)
    return plan


def _definition(farm, report_type, export_format, block_id=None):
    filters = {"date_from": "2026-07-01", "date_to": "2026-07-31"}
    if block_id:
        filters["farm_block_id"] = str(block_id)
    return ReportDefinition.objects.create(
        farm=farm, name="Test report", report_type=report_type, export_format=export_format, filters=filters
    )


@pytest.mark.django_db
def test_generate_report_produces_pdf_for_production_report(farm, block_a, harvest_data):
    definition = _definition(farm, ReportDefinition.TYPE_PRODUCTION, ReportDefinition.FORMAT_PDF, block_a.id)
    run = ReportRun.objects.create(definition=definition)
    generate_report(run)
    run.refresh_from_db()
    assert run.status == ReportRun.STATUS_READY, run.error_message
    assert run.file
    run.file.open("rb")
    content = run.file.read()
    run.file.close()
    assert content.startswith(b"%PDF")


@pytest.mark.django_db
def test_generate_report_produces_xlsx_for_tree_health_report(farm, block_a):
    definition = _definition(farm, ReportDefinition.TYPE_TREE_HEALTH, ReportDefinition.FORMAT_XLSX, block_a.id)
    run = ReportRun.objects.create(definition=definition)
    generate_report(run)
    run.refresh_from_db()
    assert run.status == ReportRun.STATUS_READY, run.error_message
    assert run.file
    run.file.open("rb")
    content = run.file.read()
    run.file.close()
    assert content[:2] == b"PK"  # xlsx is a zip archive


@pytest.mark.django_db
def test_unimplemented_report_type_fails_with_clear_message(farm, block_a):
    definition = _definition(farm, ReportDefinition.TYPE_COMPLIANCE, ReportDefinition.FORMAT_PDF, block_a.id)
    run = ReportRun.objects.create(definition=definition)
    generate_report(run)
    run.refresh_from_db()
    assert run.status == ReportRun.STATUS_FAILED
    assert "not implemented" in run.error_message.lower() or "isn't implemented" in run.error_message.lower()
    assert not run.file


@pytest.mark.django_db
def test_manager_can_trigger_on_demand_run_via_api(as_manager, farm, block_a, harvest_data):
    definition = _definition(farm, ReportDefinition.TYPE_PRODUCTION, ReportDefinition.FORMAT_XLSX, block_a.id)
    response = as_manager.post(f"/api/reports/{definition.id}/run/")
    assert response.status_code == status.HTTP_200_OK, response.data
    assert response.data["status"] == ReportRun.STATUS_READY
    assert response.data["file"]  # a downloadable URL is present


@pytest.mark.django_db
def test_manager_can_create_definition_pic_block_cannot(as_manager, as_pic_block, farm):
    response = as_manager.post(
        "/api/reports/",
        {"farm": str(farm.id), "name": "Monthly production", "report_type": "production", "export_format": "pdf"},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data

    forbidden = as_pic_block.post(
        "/api/reports/",
        {"farm": str(farm.id), "name": "attempted", "report_type": "production", "export_format": "pdf"},
    )
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_pic_block_can_read_and_run_definitions_for_their_block(as_pic_block, farm, block_a, harvest_data):
    # block_a.pic == pic_block per conftest.py — read/run access follows
    # farm visibility, not the Manager-only create restriction.
    definition = _definition(farm, ReportDefinition.TYPE_PRODUCTION, ReportDefinition.FORMAT_PDF, block_a.id)
    list_response = as_pic_block.get("/api/reports/")
    assert list_response.status_code == status.HTTP_200_OK
    assert str(definition.id) in [row["id"] for row in list_response.data["results"]]

    run_response = as_pic_block.post(f"/api/reports/{definition.id}/run/")
    assert run_response.status_code == status.HTTP_200_OK
    assert run_response.data["status"] == ReportRun.STATUS_READY


@pytest.mark.django_db
def test_manager_cannot_reassign_a_report_definition_to_a_farm_they_do_not_manage(as_manager, farm):
    """Regression test for a gap found alongside the FK-scope audit:
    ReportDefinitionViewSet checked the Manager-only rule on create() but
    never overrode perform_update(), so PATCHing an existing definition's
    `farm` to one the requester doesn't manage previously went through
    unchecked (see apps/reporting/views.py).
    """
    definition = _definition(farm, ReportDefinition.TYPE_PRODUCTION, ReportDefinition.FORMAT_PDF)
    other_farm = Farm.objects.create(name="A farm this manager has no role on")

    response = as_manager.patch(f"/api/reports/{definition.id}/", {"farm": str(other_farm.id)}, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    definition.refresh_from_db()
    assert definition.farm_id == farm.id
