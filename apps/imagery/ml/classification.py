"""
Health classification model loading — mirrors detection.py's pattern
exactly (stub default, lazy real-model import, single get_classifier()
entry point). See docs/06-ml-pipeline.md § Function 2 for the ResNet-18
inference shape this is modeled on.

A classifier is any callable: (crop: PIL.Image) -> (label: str, confidence: float).
"""
from django.conf import settings

HEALTH_LABELS = ["healthy", "small", "yellow"]


def stub_classifier(crop):
    """Deliberately NOT a random/fixed-label guess presented as a real
    prediction — returns the lowest-confidence-looking answer (0.0) so
    downstream code that filters on a confidence threshold
    (docs/06-ml-pipeline.md step 4: "results below a confidence threshold
    ... surface in the review queue rather than being auto-accepted")
    correctly routes every stub result to human review instead of treating
    it as a real automated classification.
    """
    return "healthy", 0.0


def _load_real_classifier(model_path):
    """torch/torchvision imports deferred inside this function — see
    detection.py's module docstring for why this matters for local dev
    without trained weights.
    """
    import torch  # noqa: PLC0415
    from torchvision import models, transforms  # noqa: PLC0415

    model = models.resnet18(weights=None)
    model.fc = torch.nn.Linear(model.fc.in_features, len(HEALTH_LABELS))
    model.load_state_dict(torch.load(model_path, map_location="cpu"))
    model.eval()

    preprocess = transforms.Compose(
        [
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )

    def classifier(crop):
        x = preprocess(crop).unsqueeze(0)
        with torch.no_grad():
            probs = torch.softmax(model(x), dim=1)[0]
        idx = int(probs.argmax())
        return HEALTH_LABELS[idx], float(probs[idx])

    return classifier


def get_classifier():
    if not settings.CLASSIFICATION_MODEL_PATH:
        return stub_classifier
    return _load_real_classifier(settings.CLASSIFICATION_MODEL_PATH)
