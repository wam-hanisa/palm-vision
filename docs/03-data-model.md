# Data model

Consolidated reference across every module. Field lists here are the source of truth for model definitions — if `02-build-plan.md` or another doc's prose disagrees with this file on a field name, this file wins.

## Core: farms, blocks, trees

```mermaid
erDiagram
    FARM ||--o{ FARM_BLOCK : contains
    FARM ||--o{ IMAGE : "uploaded to (not block-specific)"
    FARM_BLOCK ||--o{ TREE : contains
    FARM_BLOCK ||--o{ USER_FARM_ROLE : "scopes roles to"

    FARM_BLOCK {
        uuid id
        fk farm_id
        string name
        polygon boundary
        string commodity
        float area
        int year
        float tree_match_threshold_m "nullable, falls back to global default"
    }
    TREE {
        uuid id
        fk farm_block_id
        point location
        string source "manual or drone_detected"
        string status "active or removed (soft delete)"
        string current_health "denormalized latest classification"
        fk created_by
        fk updated_by
        datetime updated_at
    }
    USER_FARM_ROLE {
        uuid id
        fk user_id
        fk farm_id
        string role "manager or pic_block; super_admin is global, not scoped"
    }
```

Notes:
- `Tree.status` is a soft-delete flag — never hard-delete, since detection matching and history depend on removed trees still existing as rows.
- `Tree.source` distinguishes manually-added trees from ones confirmed out of the detection pipeline — keep this after confirmation too, for later auditing of which source is more reliable.
- `Tree.current_health` is denormalized for fast dashboard reads; the full history lives in `TreeHealthClassification` (below), which is the source of truth for trend queries.
- `FarmBlock.boundary` must pass a validity check (no self-intersection) before save.
- `UserFarmRole` scopes Manager/PIC Block role assignments to specific farms; Super Admin is unscoped by definition.

## Imagery, detection, classification

```mermaid
erDiagram
    IMAGE ||--o{ IMAGE_PROCESSING_RUN : "clipped and run per block"
    FARM_BLOCK ||--o{ IMAGE_PROCESSING_RUN : "scoped to"
    IMAGE_PROCESSING_RUN ||--o{ TREE_DETECTION : produces
    TREE ||--o{ TREE_DETECTION : "matched to (nullable)"
    TREE_DETECTION ||--o| TREE_HEALTH_CLASSIFICATION : classified_as
    MODEL_VERSION ||--o{ IMAGE_PROCESSING_RUN : "used by"

    IMAGE {
        uuid id
        fk farm_id
        file raw_file
        datetime captured_at
        string georeference_status "pending / done / failed"
        polygon footprint "computed after georeferencing"
    }
    IMAGE_PROCESSING_RUN {
        uuid id
        fk image_id
        fk farm_block_id "which block this run was clipped to"
        string status "pending / clipping / detecting / classifying / ready / failed"
        fk detection_model_version
        fk classification_model_version
        fk triggered_by
        int detected_tree_count
        int new_candidate_count
        datetime started_at
        datetime completed_at
    }
    TREE_DETECTION {
        uuid id
        fk processing_run_id
        fk tree_id "null until matched or confirmed"
        point detected_location
        float detection_confidence
        string match_status "matched / new_candidate / ambiguous / rejected"
        float matched_distance_m
    }
    TREE_HEALTH_CLASSIFICATION {
        uuid id
        fk tree_detection_id
        string health_label "healthy / unknown / yellow"
        float confidence
        boolean reviewed
        fk reviewed_by
        string corrected_label "set if a human overrode it"
    }
    MODEL_VERSION {
        uuid id
        string model_type "detection or classification"
        string status "staging / production / retired"
        date trained_at
        string dataset_version
        fk trained_by
        json validation_metrics
    }
```

Notes:
- `Image` belongs to a `Farm`, not a single block — one file can be processed against several blocks independently via multiple `ImageProcessingRun` rows.
- Every `TREE_DETECTION` and `TREE_HEALTH_CLASSIFICATION` write is a new row, never an overwrite — this is what gives you a real history per tree over time.
- `matched_distance_m` and the matching threshold logic live on `FarmBlock.tree_match_threshold_m` (see core model above) — don't hardcode a global constant.
- `ModelVersion.status` drives the shadow-mode/promotion workflow described in `06-ml-pipeline.md` — `IMAGE_PROCESSING_RUN` should reference the specific `ModelVersion` used, not a bare version string.

## Harvest & fertilizer operations

```mermaid
erDiagram
    FARM_BLOCK ||--o{ HARVEST : plans
    FARM_BLOCK ||--o{ FERTILIZER : plans
    HARVEST ||--o{ ACTUAL_HARVEST : executed_as
    FERTILIZER ||--o{ ACTUAL_FERTILIZER : executed_as
    ACTUAL_HARVEST ||--o{ HARVEST_TREE : "per-tree yield"
    ACTUAL_FERTILIZER ||--o{ FERTILIZER_TREE : "per-tree application"
    TREE ||--o{ HARVEST_TREE : yields
    TREE ||--o{ FERTILIZER_TREE : receives

    HARVEST {
        uuid id
        fk farm_block_id
        date assessment_date
        float assessment_quantity
        string status "planned / created_confirmed / on_progress / done, etc."
    }
    ACTUAL_HARVEST {
        uuid id
        fk harvest_id
        date date
        float weight_kg "authoritative total (kg); wins over tree-level sum whenever set"
        float quantity "optional bunch count, supplementary only"
    }
    HARVEST_TREE {
        uuid id
        fk actual_harvest_id
        fk tree_id
        float weight_kg "authoritative per-tree value (kg)"
        float quantity "optional bunch count, supplementary only"
        point recorded_location "GPS at capture time"
        fk recorded_by
        datetime recorded_at
    }
    FERTILIZER {
        uuid id
        fk farm_block_id
        date assessment_date
        string status
    }
    ACTUAL_FERTILIZER {
        uuid id
        fk fertilizer_id
        date date
    }
    FERTILIZER_TREE {
        uuid id
        fk actual_fertilizer_id
        fk tree_id
        float quantity
    }
```

Notes:
- `ActualHarvest.weight_kg` (kg — every harvest record's authoritative unit, not bunch count) is authoritative whenever it's manually set, full stop; it only falls back to summing `HarvestTree.weight_kg` rows when no manual total was ever given. `quantity` (bunch count) is optional supplementary detail on both models, never part of this derivation — see `apps/operations/models.py`'s `get_effective_quantity()`/`effective_quantity` for the exact remainder-split rule when some but not all tree rows are individually measured. `ActualFertilizer`/`FertilizerTree` mirror this exactly, already kg-native (no separate bunch-count field ever existed there).
- Status transitions on `Harvest`/`Fertilizer` are `TextChoices` enums, enforced through DRF permission classes on every state-changing endpoint — approval specifically requires the actor to hold the right role/permission, checked server-side, every time.

## Compliance & traceability

See `07-compliance.md` for the full model and field list — reproduced there rather than duplicated here since that doc also carries the regulatory context (EUDR/ISPO specifics) the fields exist to satisfy.

## Reporting & notifications

See `08-reporting.md` and `12-security-ops.md` § Alerting & Notifications for `ReportDefinition`/`ReportRun` and `Notification` model details.
