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

**Current state: a real trained model, not a downloaded pretrained one.** `apps/imagery/ml/detection.py`'s `_load_real_detector()` loads a Faster R-CNN (ResNet50-FPN-v2, via `torchvision.models.detection`), single foreground class ("oilpalm"), trained by the project owner directly through the sibling `palm-vision-ml` project's `tree_health_pipeline` on the Roboflow "tree-detection" dataset (197,332 boxes across 2,724 images). One fold of a configured 5-fold cross-validation actually ran (6 epochs; `max_folds=1` stopped the run after fold 0, so this is validated on one train/val split, not full k-fold — worth knowing before trusting the numbers below as more than a single-split estimate). Held-out metrics from that fold: detection precision 0.989, recall 0.961 (mean per-image), tree-count MAE 6.11 versus a naive-baseline MAE of 11.10. These are the model's own held-out numbers on data drawn from the source dataset — not yet confirmed against this project's own farm imagery.

**This supersedes the project's earlier recommendation of a downloaded Roboflow YOLO model** (Oil Palm Tree Crown Detection from Aerial Image, YOLOv11n) — that model was never actually wired in (`DETECTION_MODEL_PATH` stayed unset the entire time it was the documented recommendation), and `apps/imagery/ml/detection.py`'s loader no longer understands the Ultralytics checkpoint format at all; it's specifically built around this project's own Faster R-CNN checkpoint shape.

**Tiling is load-bearing, not an optimization.** This model was trained on tiles around 1024×1024px (the source dataset's own median image size) — a real block-clipped orthomosaic region is typically much larger. `_load_real_detector()` ports the training pipeline's own tiling design (`ingestion.py`'s `OrthomosaicLoader.tile_generator()`: 1024px tiles, 128px overlap) for the already-in-memory clipped array, then applies a world-coordinate NMS pass (ported from `crown_detection.py`'s `merge_detections_world_nms()`) to undo the double-counting that overlapping tiles would otherwise cause for any crown straddling a tile seam. Skipping tiling isn't a smaller/simpler version of this feature — it either silently destroys small-object detail via the model's internal downsampling, or reproduces the CUDA out-of-memory crash actually hit once during this model's own training (its own post-training evaluation step ran the model directly against a full, untiled image and crashed — see BUILD_STATUS.md's matching update for the traceback).

**A real coordinate-reprojection bug, fixed while wiring this in, not left for later.** A detector's contract is `(clipped_array, transform, crs) -> list of (lon, lat, confidence)` in true EPSG:4326 coordinates. `transform * (px, py)` only ever produces a point in the raster's own native CRS (UTM meters, for a projected orthomosaic) — the previous YOLO-based loader fed that straight into the contract unreprojected, silently wrong for any raster not already in EPSG:4326, despite this function's own docstring always promising geographic coordinates. Never exercised end-to-end before now (no detector was ever actually configured), but fixed here — see `_reproject_point_to_lonlat()` — rather than carried forward into the first detector that actually runs for real.

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

### Function 2 — classify health (real trained ResNet-18, 10-channel input)

Runs on the cropped patch for each `matched` detection (and any `new_candidate` once confirmed) — not the whole image.

**Current state: a real from-scratch-trained model, not a sketch or an adapter.** `apps/imagery/ml/classification.py`'s `_load_real_classifier()` loads `trained_models/oil_palm_health_classifier.pth` — a ResNet-18 the project owner trained directly (via the sibling `palm-vision-ml` project's `tree_health_pipeline`) on the full 352,551-box Roboflow "Oil-Palm-Health" dataset (CC BY 4.0), with a tile-ID-grouped 80/20 spatial split and class-weighted loss for the dataset's ~413:1 class imbalance. Validation metrics embedded in the checkpoint itself (18 epochs): accuracy 0.957, macro F1 0.806, against a 0.288-accuracy/0.089-macro-F1 majority-class baseline for the same split. These are the model's own numbers on data drawn from the source dataset — not yet confirmed against this project's own farm imagery (see BUILD_STATUS.md's matching update for what's still open).

**The input contract is genuinely different from a plain RGB classifier, and every part of it matters:**

- **10 input channels**: RGB + 7 RGB-derived vegetation indices (VARI, GLI, ExG, ExGR, NGRDI, MGRVI, RGBVI — `apps/imagery/ml/vegetation_indices.py`, a faithful port of the training pipeline's own module) stacked in that exact order. The first conv layer's weights are shaped `(64, 10, 7, 7)`, not `(64, 3, 7, 7)` — feeding this model plain RGB would silently corrupt every prediction, not just reduce accuracy.
- **96×96 patches** (the checkpoint's own `patch_size` field, trusted at load time over any hardcoded constant).
- **Normalization**: RGB channels divided by 255.0 (uint8 assumed); VI channels are raw index values with NaN/Inf replaced by 0.0. No ImageNet mean/std normalization on either.
- **Why RGB-only VIs, not NDVI/NDRE**: this project's drone imagery has no near-infrared or red-edge band, so the standard reflectance-based stress indices are not computable — see `vegetation_indices.py`'s own module docstring for the full domain-constraint explanation. These seven are a weaker, visible-light proxy for greenness, useful as auxiliary CNN input features, not a stress diagnostic on their own.

**5-class model, 3-class app schema — a deliberate product decision.** The checkpoint classifies into 5 classes (HEALTHY, SMALL, YELLOW, DEAD, GRASS — the real class distribution measured directly from the source dataset's own annotations), but `Tree.current_health`/`TreeHealthClassification.HEALTH_CHOICES` only keeps three stored values: healthy/unknown/yellow. SMALL predictions map to `unknown` (an explicit mapping, not a fall-through — the model's real confidence is kept) rather than being kept as their own "small" label — asked directly, after this doc's original healthy/small/yellow scheme shipped. DEAD/GRASS predictions still route to human review — never silently forced into one of the three real labels, the same "unmapped class routes to review" convention the classifier's predecessor already established. See `apps/imagery/ml/classification.py`'s module docstring for the full reasoning and `_HEALTH_LABEL_MAP`. This is revisitable — the model already produces a dead-palm signal, it's just not surfaced as app data yet.

**History, briefly** (see BUILD_STATUS.md for the full account): this section originally sketched an untested ResNet-18 shape close to today's real one. Between that sketch and today's real model, this function was temporarily backed by an adapted Roboflow YOLO *detector* (its highest-confidence box's class, reinterpreted as a classification label) — a stopgap chosen only because no compatible pretrained crop classifier existed publicly at the time, and no from-scratch model had been trained yet. That adapter is gone; the function above is what's actually running now.

**Two real fixes for this project's own imagery, found against real Jonggol farm data, not the source dataset's held-out split**: (1) **Exposure/color normalization** — a per-channel brightness/color-balance mismatch between Jonggol's Pix4D-processed orthomosaic and the training data's own reference level pushed every crop to a spurious "SMALL" verdict at 100% confidence; `apps/imagery/ml/classification.py`'s `normalize_exposure()` corrects it, opt-in via `settings.CLASSIFICATION_NORMALIZE_EXPOSURE` (enabled for this deployment specifically, since unconditionally applying it regresses accuracy on in-distribution imagery). (2) **Ground-sample-distance normalization** — the same class of bug `_native_gsd_m()`/`DETECTION_TARGET_GSD_M` fixed for detection above, but never applied to classification: `crop_patch()` always cropped a fixed 96 NATIVE pixels straight out of the clipped array regardless of its real resolution, so at Jonggol's ~1.843cm/pixel that patch covered only ~1.77m of real ground — close-up leaf/frond texture, not a crown. `classification_native_patch_size_px()`/`CLASSIFICATION_TARGET_GSD_M` (≈15cm/pixel, derived the same way from the health-condition dataset's own ~43px median crown box) scales the native crop window so it covers roughly the same real-world ground the training patches did, before the existing resize-to-96×96 step. See `classification.py`'s own module docstring for both fixes' full writeups.

### Full pipeline sequence

1. Image finishes georeferencing → backend confirms `farm_block.boundary` is set (refuses otherwise) → Celery chains into detection, clipping first.
2. Detection writes `TreeDetection` rows (each validated within the block polygon), applies matching logic.
3. For every `matched` detection (and confirmed `new_candidate`s), classification crops the patch and writes `TreeHealthClassification`.
4. Results below a confidence threshold (e.g. 70% as a starting point, tune from there) get `reviewed=False` and surface in the review queue rather than being auto-accepted.
5. A Manager or PIC Block reviews the queue — confirming/rejecting candidates, confirming/correcting low-confidence labels. Corrections become future training data for both models.

### UI rule mirroring the backend rule

No global/bare map view for reviewing imagery or triggering detection. Every screen in this pipeline (upload, review queue, detection results) is reached by navigating Farm Block → Imagery → Image — the block is always already selected before any map or detection action is available.

## Labeling data — the real bottleneck for detection; addressed for classification

The detector still needs bounding boxes drawn around trees in sample images — no trained crown detector exists yet (see Function 1's update above; `trained_models/crown_detector_run/` has a training attempt in progress but no completed weights). The classifier's original gap (individual crops tagged with true health status) is now addressed, though not with this project's own field labels — the real trained model (Function 2 above) learned from the source Roboflow dataset's 352,551 externally-labeled boxes, not from this plantation's own confirmed/corrected detections. That's still a real gap worth tracking: the review-queue UI doubling as a labeling interface (confirming/correcting during normal operation *is* the labeling process) is how this project's *own* label data accumulates over time, and would be the basis for ever retraining on this plantation's specific conditions rather than the source dataset's. Treat the current deployed classifier as decision-support flagging for human confirmation, not an autonomous replacement, until validated over at least one full season against this project's own imagery.

## Where this runs

Both functions run as chained Celery jobs (detect → match → classify), triggered once per image upload/selection — not a standing always-on inference server. CPU inference is likely adequate for the classifier; the detector running over a full orthomosaic is the more resource-intensive of the two and is where GPU-backed workers would be needed first if processing time becomes a bottleneck at real data volume.

## Model governance & promotion workflow

Model validation and retraining is owned directly by the project owner, not a dedicated ML-ops team — keep this tooling lightweight, not a full MLOps platform.

- **`ModelVersion` registry** — one row per trained model (detection and classification versioned independently): training date, dataset version, validation metrics, who trained it, status (`staging`/`production`/`retired`). `ImageProcessingRun` references this, not a bare version string.
- **Staging before production** — a newly trained model starts in `staging` and runs in shadow mode (predictions recorded, not shown to users) against a sample of recent real images, compared against the current production model's accuracy proxy (below) before promotion.
- **Rollback** — promoting a model to `production` flips the previous one to `retired`, not deleted — still loadable, so a bad promotion is a status change, not a redeployment scramble.
- **Model performance view** — since `TreeHealthClassification` already stores `reviewed`/`corrected_label`, build a view per `model_version` showing what fraction of predictions were later corrected by a human, broken down by class. This is the data source the shadow-mode comparison runs on.
