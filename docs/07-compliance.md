# Certification & compliance module

## Why this matters now, not eventually

- **EUDR** (EU Deforestation Regulation) applies from **30 December 2026** for large/medium operators, and explicitly covers palm oil. It requires plot-level geolocation — a GeoJSON polygon for any plot ≥4 hectares, coordinates to at least six decimal places — plus proof the land wasn't deforested after a **31 December 2020 cutoff**.
- **ISPO** (Indonesian Sustainable Palm Oil) certification is mandatory for Indonesian palm oil operators; Indonesia's Ministry of Industry has set a **March 2027 deadline** for downstream businesses to hold it.

Both are close enough that this belongs early in the build order if either applies to the operation — see `02-build-plan.md` Phase 6.

**Head start already built in:** `FarmBlock.boundary` (see `03-data-model.md`) is already a real PostGIS polygon, satisfying EUDR's geolocation format requirement structurally. This module is mostly about attaching the right documents and metadata to geolocation data already being captured, not building geolocation from scratch.

## Data model

```mermaid
erDiagram
    FARM ||--o{ FARM_BLOCK : contains
    FARM_BLOCK ||--o| DEFORESTATION_BASELINE : "has one"
    FARM ||--o{ COMPLIANCE_DOCUMENT : "attached to farm or block"
    FARM_BLOCK ||--o{ COMPLIANCE_DOCUMENT : "attached to farm or block"
    FARM_BLOCK ||--o{ COMPLIANCE_AUDIT : audited
    COMPLIANCE_AUDIT_TEMPLATE ||--o{ COMPLIANCE_AUDIT : "based on"
    COMPLIANCE_AUDIT_TEMPLATE ||--o{ CHECKLIST_ITEM : defines
    COMPLIANCE_AUDIT ||--o{ AUDIT_RESULT : contains
    CHECKLIST_ITEM ||--o{ AUDIT_RESULT : "answered by"
    FARM_BLOCK ||--o{ HARVEST_BATCH : "sourced from"
    HARVEST_BATCH ||--o{ ACTUAL_HARVEST : aggregates
    FARM ||--o{ GRIEVANCE_RECORD : logged_against

    COMPLIANCE_DOCUMENT {
        uuid id
        string document_type "land_title / AMDAL_permit / ISPO_cert / other"
        file document_file
        date issued_date
        date expiry_date
        string issuing_authority
        string status "valid / expiring_soon / expired, derived from expiry_date"
    }
    DEFORESTATION_BASELINE {
        uuid id
        fk farm_block_id
        date cutoff_date "2020-12-31 for EUDR"
        string source "e.g. satellite baseline reference"
        string status "deforestation_free / flagged / under_review"
        fk verified_by
        datetime verified_at
    }
    COMPLIANCE_AUDIT {
        uuid id
        fk farm_block_id
        fk template_id
        date audit_date
        string auditor_name
        string auditor_type "internal / third_party"
        string overall_status
        date next_audit_due
    }
    HARVEST_BATCH {
        uuid id
        date period_start
        date period_end
        float total_quantity
        float total_weight_kg
        string export_status "for due-diligence statement generation"
    }
    GRIEVANCE_RECORD {
        uuid id
        date date_reported
        string description
        string status "open / investigating / resolved"
        text resolution_notes
    }
```

## EUDR-specific functions

- **Due diligence statement export** — given a `HarvestBatch` and its source block(s), assemble: the block polygon (validated for 6-decimal coordinate precision), a deforestation-free status from `DeforestationBaseline`, and placeholder risk-assessment/supplier/HS-code fields for a compliance officer to complete. This generates the *data package*, not an automated legal filing — never build a "submit directly to authorities" button without legal/compliance validation of the output first.
- **Deforestation baseline tracking** — per block, whether land was clear of forest cover as of the 31 December 2020 cutoff. Needs a real evidence source (a forest-monitoring data provider API, or an internally-maintained satellite baseline) — this is an external-data integration decision, not something derivable from block boundaries alone. Treat `FOREST_MONITORING_API_KEY` (see `.env.example`) as optional; when unset, `DeforestationBaseline.status` defaults to `under_review`, not a guessed value.
- **Five-year retention** on due diligence records, per EUDR — compliance documents need a stricter storage policy than routine data (see `12-security-ops.md` § Backup & Disaster Recovery Policy).

## ISPO / domestic certification functions

- **Document management with expiry tracking** — `ComplianceDocument` covers land title (HGU), environmental permits (AMDAL), the ISPO certificate itself, and others, each with a computed status (valid/expiring_soon/expired). The exact document categories an ISPO auditor requires should be verified directly with legal/compliance counsel, not assumed from this spec.
- **Configurable audit/checklist module**, not hardcoded to ISPO — `ComplianceAuditTemplate` + `ChecklistItem` define a reusable checklist per certification scheme (ISPO now, RSPO later if relevant). Each `ComplianceAudit` records a result per checklist item, with optional evidence, auditor info, and `next_audit_due`. Deliberately generic since certification requirements do get revised over time.
- **Grievance log** — land/community conflict records (date, description, status, resolution), which certification audits commonly expect evidence of a functioning complaints mechanism for.

## Traceability, scoped to "harvest logged in the field"

System scope currently ends at harvest logged in the field — no mill/downstream connection. `HarvestBatch` is the seam for that later, without building it now: an aggregation of `ActualHarvest`/`HarvestTree` records over a period and source block(s), exportable with geolocation and certification status attached. Today, that export is the traceability record — proof a given quantity of FFB came from specific, geolocated, (ideally) certified blocks within a date range.

## Block-level compliance, tree-level operations — not a conflict

Certification and traceability requirements (ISPO, EUDR) attach to geolocated plots — `FarmBlock`s — not individual trees; that's the unit these schemes actually audit against. Tree-level detail (health classification, per-tree yield) stays exactly as designed elsewhere and rolls up cleanly into block-level `HarvestBatch` totals. Don't try to force compliance records down to tree granularity.

## Compliance dashboard

- **Expiry alerts** — a document/certification nearing expiry pushes a notification (see `12-security-ops.md` § Alerting & Notifications), doesn't wait for someone to notice on a dashboard.
- **Model performance view** — see `06-ml-pipeline.md` § Model governance & promotion workflow.
