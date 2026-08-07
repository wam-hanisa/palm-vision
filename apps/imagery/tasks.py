"""Thin Celery wrappers around apps/imagery/services.py. No pipeline logic
lives here — that's deliberate, see services.py's module docstring for why.
"""
from celery import shared_task

from . import services
from .models import Image, ImageProcessingRun


@shared_task
def georeference_image_task(image_id):
    image = Image.objects.get(id=image_id)
    services.georeference_image(image)


@shared_task
def run_processing_task(run_id):
    run = ImageProcessingRun.objects.get(id=run_id)
    services.run_processing(run)
