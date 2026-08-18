"""Thin Celery wrappers around apps/imagery/services.py. No pipeline logic
lives here — that's deliberate, see services.py's module docstring for why.
"""
from celery import shared_task

from . import services
from .models import Image, ImageProcessingRun


@shared_task
def georeference_image_task(image_id):
    image = Image.objects.get(id=image_id)
    # COG conversion first — see convert_to_cog()'s docstring. Never
    # raises (records is_cog/cog_error on failure), so this always runs
    # regardless of outcome, same as generate_preview() below.
    services.convert_to_cog(image)
    services.georeference_image(image)
    # Only attempt a preview once the raster's actually openable and
    # georeferenced — if georeference_image() itself failed (e.g. corrupt
    # file, no CRS), a preview attempt would just fail the same way for
    # the same reason; no point trying and no point recording a second,
    # redundant error message for the same root cause.
    if image.georeference_status == Image.GEOREFERENCE_DONE:
        services.generate_preview(image)


@shared_task
def run_processing_task(run_id):
    run = ImageProcessingRun.objects.get(id=run_id)
    services.run_processing(run)


@shared_task
def apply_storage_lifecycle_task():
    """Celery Beat periodic task — see CELERY_BEAT_SCHEDULE in
    palmvision/settings/base.py and services.apply_storage_lifecycle()'s
    own docstring for the full policy this implements.
    """
    services.apply_storage_lifecycle()


@shared_task
def mark_stale_processing_runs_failed_task():
    """Celery Beat periodic task — see CELERY_BEAT_SCHEDULE in
    palmvision/settings/base.py and
    services.mark_stale_processing_runs_failed()'s own docstring for why
    this exists: a run stuck mid-pipeline because its worker process was
    killed outright (not a raised exception) has no other way to ever
    leave that state.
    """
    services.mark_stale_processing_runs_failed()
