"""
Tree detection model loading. docs/02-build-plan.md Phase 5: "it's
acceptable to stub these with a trivial placeholder ... if no trained
model exists yet — the pipeline's correctness (clipping, matching, status
transitions) needs to be verifiable independent of having a
production-quality model."

`torch`/`torchvision`/real-model imports are deliberately deferred inside
`_load_real_detector()`, not at module level — importing this module (which
happens on every Celery worker boot and every test run) must not require
torch to be installed just because DETECTION_MODEL_PATH happens to be
unset, which is the expected state for local dev until real trained
weights exist.

A detector is any callable: (clipped_array, transform, crs) -> list of
(lon, lat, confidence) tuples in the raster's native CRS pixel space
already converted to geographic coordinates by the caller — see
apps/imagery/services.py run_detection() for the exact contract.
"""
from django.conf import settings


def stub_detector(clipped_array, transform, crs):
    """Returns no detections. This is a deliberate, honest default — a
    detector that invents fake tree positions would be worse than one that
    admits it has nothing to offer yet. Tests that need to exercise the
    matching/status-transition logic downstream of detection should inject
    a fake detector directly (see apps/imagery/tests.py), not rely on this
    stub returning anything.
    """
    return []


def _load_real_detector(model_path):
    """YOLOv8 loading, per docs/06-ml-pipeline.md's model choice. Imports
    ultralytics lazily — only reached when DETECTION_MODEL_PATH is set to
    a real weights file, which isn't the case in local dev without a
    trained model.
    """
    from ultralytics import YOLO  # noqa: PLC0415 — deliberately deferred, see module docstring

    model = YOLO(model_path)

    def detector(clipped_array, transform, crs):
        results = model(clipped_array)
        detections = []
        for box in results[0].boxes:
            x_center_px, y_center_px = box.xywh[0][:2].tolist()
            confidence = float(box.conf[0])
            lon, lat = transform * (x_center_px, y_center_px)
            detections.append((lon, lat, confidence))
        return detections

    return detector


def get_detector():
    """The single entry point apps/imagery/services.py should call —
    never import stub_detector or _load_real_detector directly, so
    swapping in a real model later is a one-place change.
    """
    if not settings.DETECTION_MODEL_PATH:
        return stub_detector
    return _load_real_detector(settings.DETECTION_MODEL_PATH)
