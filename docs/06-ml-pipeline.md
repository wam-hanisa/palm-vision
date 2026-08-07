# Imagery upload, detection, classification, and model governance

## Drone imagery upload workflow

Large orthomosaics (hundreds of MB to several GB) don't fit the small-record offline sync queue — this is a separate path.

**Where it happens:** drone flights capture to local storage (SD card/tablet), not sent anywhere live. Upload happens once back on real bandwidth, from the office dashboard — not the field PWA.

**Steps:**

1. **Capture** — drone flies the block; images/orthomosaic land on local storage.
2. **Upload, at the farm level** — the operator uploads at the `Farm` level, not tied to one block, since one file can cover several blocks. Chunked/resumable upload (e.g. `django-chunked-upload` or a `tus`-protocol server) straight to storage via pre-signed URLs.
3. **Automatic georeferencing** — a Celery task reads embedded GPS/GDAL metadata via `rasterio` and populates the file's footprint as a real PostGIS polygon. (The original Laravel app had an unused, unwired method meant to do this — this finishes that feature.) Manual bounding-box entry is kept only as a fallback.
4. **Tiling for the map view** — a background job generates web map tiles (`rio-tiler`/`titiler`, or a simpler downsampled preview) so imagery displays quickly without loading the full file.
5. **Select which block(s) to process against** — once georeferenced, the file's footprint is checked against every block on the farm (`boundary__intersects`) to suggest overlapping blocks. An operator explicitly picks one or more and triggers a processing run per block — nothing runs automatically across all overlapping blocks by default.
6. **Processing status** is tracked per `ImageProcessingRun`, not per `Image` — the same file can show "Block 12: ready, Block 13: still processing" independently.

## Detection and classification are two separate functions

Not one combined step — this is a deliberate choice so the pipeline catches trees that were planted but never entered into the registry, not just confirms known ones. It's more work (two models, two labeled datasets) than a classify-only approach; that trade-off has already been accepted.

**Prior art:** [Oil Palm Tree Detection and Health Classification on High-Resolution Imagery Using Deep Learning (MDPI Agriculture, 2021)](https://www.mdpi.com/2077-0472/11/2/183) — Faster R-CNN with VGG-16/ResNet-50 backbones on UAV imagery. Detection scored ~97.7% F1; health classification scored 57.1%–95.3% F1 depending on setup. Health classification is meaningfully harder than detection — calibrate expectations accordingly, and don't treat early classification accuracy numbers as a sign something is broken if they land on the lower end of that range.

### Function 1 — detect trees, clipped to the selected block's polygon

Block-scoping has two distinct layers, both required:

- **Organizational** — `Image` belongs to a `Farm`; a specific `ImageProcessingRun` ties it to one `FarmBlock`. Already enforced by the schema.
- **Spatial** — the raster can extend beyond the block's true boundary (flight overshoot, buffer zones, neighboring blocks). "Tagged to block X" does not guarantee the detector only sees block X's trees unless the image is spatially clipped first.

**Implementation:** `rasterio.mask.mask()` masks the georeferenced raster against `farm_block.boundary` before the detector ever sees it — clipping is primary, not a post-filter. **Precondition: a block must have a drawn boundary polygon before any image belonging to it can be processed.** If null, refuse and surface "draw this block's boundary first" — never fall back to processing the unclipped image.

**Belt-and-suspenders check:** every resulting detection's GPS point is also validated against the block polygon with a PostGIS `within` lookup before being persisted, catching any clipping edge artifacts.

**Model:** a real-time object detector such as YOLOv8 (or Faster R-CNN, more accurate but slower), fine-tuned on labeled bounding boxes around palm tree crowns. Needs its own labeled dataset — separate from classification labels.

### Matching against the existing tree registry

For each detection, query existing `Tree` rows in that block within a configurable distance threshold, using PostGIS's indexed `dwithin` lookup (not a plain distance calculation):

```python
Tree.objects.filter(
    farm_block=block,
    location__dwithin=(detected_point, D(m=threshold)),
)
```

- **One match within threshold** → `match_status = matched`, linked to the existing tree.
- **No match within threshold** → `match_status = new_candidate`, surfaced in the review queue — never auto-created.
- **Two or more equally-close matches** → `match_status = ambiguous`, also routed to manual review.

**Threshold:** must be well below actual inter-tree spacing or every detection will "match" the nearest tree regardless of correctness. Configurable per `FarmBlock` (`tree_match_threshold_m`), falling back to a global default — tune against real imagery, don't hardcode a guessed value as final.

**Edge case:** a detection near a soft-deleted (`status=removed`) tree's old location should be flagged `ambiguous` ("possible regrowth"), not auto-matched to the dead record or auto-created as new — that's a human judgment call.

### Function 2 — classify health (ResNet-18)

Runs on the cropped patch for each `matched` detection (and any `new_candidate` once confirmed) — not the whole image. A 3-class classifier (healthy/small/yellow) is exactly what ResNet-18 is suited to; ~11M parameters, CPU-inference-viable at this volume, transfer learning from ImageNet weights.

Illustrative inference shape (not tested, just the pattern — detection follows the same "load model once, run inference, write a result row" shape):

```python
import torch
from torchvision import models, transforms
from PIL import Image

model = models.resnet18(weights=None)
model.fc = torch.nn.Linear(model.fc.in_features, 3)
model.load_state_dict(torch.load("palm_health_resnet18.pt", map_location="cpu"))
model.eval()

preprocess = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

def classify_tree_crop(crop: Image.Image) -> tuple[str, float]:
    labels = ["healthy", "small", "yellow"]
    x = preprocess(crop).unsqueeze(0)
    with torch.no_grad():
        probs = torch.softmax(model(x), dim=1)[0]
    idx = int(probs.argmax())
    return labels[idx], float(probs[idx])
```

### Full pipeline sequence

1. Image finishes georeferencing → backend confirms `farm_block.boundary` is set (refuses otherwise) → Celery chains into detection, clipping first.
2. Detection writes `TreeDetection` rows (each validated within the block polygon), applies matching logic.
3. For every `matched` detection (and confirmed `new_candidate`s), classification crops the patch and writes `TreeHealthClassification`.
4. Results below a confidence threshold (e.g. 70% as a starting point, tune from there) get `reviewed=False` and surface in the review queue rather than being auto-accepted.
5. A Manager or PIC Block reviews the queue — confirming/rejecting candidates, confirming/correcting low-confidence labels. Corrections become future training data for both models.

### UI rule mirroring the backend rule

No global/bare map view for reviewing imagery or triggering detection. Every screen in this pipeline (upload, review queue, detection results) is reached by navigating Farm Block → Imagery → Image — the block is always already selected before any map or detection action is available.

## Labeling data — the real bottleneck, not the model

The detector needs bounding boxes drawn around trees in sample images; the classifier needs individual crops tagged with true health status. Neither exists yet — the original app only ever captured block-level counts. As a rough starting estimate (not a guarantee — depends on how visually distinct the health classes are and how cluttered canopies are for detection), transfer learning with ResNet-18 often gets usable classification results from a few hundred labeled crops per class; the detector typically needs more. Plan on: the review-queue UI doubling as the labeling interface (confirming/correcting during normal operation *is* the labeling process), and treating early deployed models as decision-support flagging candidates for human confirmation, not an autonomous replacement, until validated over at least one full season.

## Where this runs

Both functions run as chained Celery jobs (detect → match → classify), triggered once per image upload/selection — not a standing always-on inference server. CPU inference is likely adequate for the classifier; the detector running over a full orthomosaic is the more resource-intensive of the two and is where GPU-backed workers would be needed first if processing time becomes a bottleneck at real data volume.

## Model governance & promotion workflow

Model validation and retraining is owned directly by the project owner, not a dedicated ML-ops team — keep this tooling lightweight, not a full MLOps platform.

- **`ModelVersion` registry** — one row per trained model (detection and classification versioned independently): training date, dataset version, validation metrics, who trained it, status (`staging`/`production`/`retired`). `ImageProcessingRun` references this, not a bare version string.
- **Staging before production** — a newly trained model starts in `staging` and runs in shadow mode (predictions recorded, not shown to users) against a sample of recent real images, compared against the current production model's accuracy proxy (below) before promotion.
- **Rollback** — promoting a model to `production` flips the previous one to `retired`, not deleted — still loadable, so a bad promotion is a status change, not a redeployment scramble.
- **Model performance view** — since `TreeHealthClassification` already stores `reviewed`/`corrected_label`, build a view per `model_version` showing what fraction of predictions were later corrected by a human, broken down by class. This is the data source the shadow-mode comparison runs on.
