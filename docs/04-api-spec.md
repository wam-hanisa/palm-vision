# API specification

Representative, not exhaustive — extend the same pattern to any model not listed here (standard DRF list/detail/create/update viewsets, scoped by role). All endpoints require JWT auth unless noted.

| Method | Endpoint | Description | Notes |
|---|---|---|---|
| POST | `/api/token/` | Obtain JWT (login) | No auth required |
| POST | `/api/token/refresh/` | Refresh JWT | |
| GET | `/api/farm-blocks/` | List blocks visible to the current user | PIC Block sees only blocks they're assigned via `UserFarmRole`; Manager sees blocks in their scoped farm(s); Super Admin sees all |
| GET | `/api/farm-blocks/{id}/trees/` | List trees in a block | For offline pre-caching before heading to the field |
| POST/PATCH/DELETE | `/api/trees/`, `/api/trees/{id}/` | Field-staff tree CRUD | Scoped to the requesting PIC Block's assigned block(s); works offline via client-generated UUID |
| POST | `/api/fertilizer-plans/{id}/actuals/` | Log an actual fertilizer application | Offline-queueable |
| POST | `/api/harvest-plans/{id}/actuals/` | Log an actual harvest | Offline-queueable; supports both block-level and tree-level (nested `HarvestTree` rows) payloads |
| POST | `/api/harvest-plans/{id}/approve/` | Approve a harvest plan | Manager-only, enforced via DRF permission class; online-only action, not offline-queueable |
| POST | `/api/fertilizer-plans/{id}/approve/` | Approve a fertilizer plan | Same rules as harvest approval |
| POST | `/api/farms/{farm_id}/images/` | Upload imagery | Farm-level, not block-specific; chunked/resumable upload |
| GET | `/api/images/{id}/overlapping-blocks/` | Suggest which blocks a file's footprint intersects | Computed after georeferencing completes |
| POST | `/api/farm-blocks/{block_id}/images/{image_id}/process/` | Start an `ImageProcessingRun` | Clips this image to this block and runs detect + classify; refuses if the block has no boundary polygon |
| GET | `/api/farm-blocks/{block_id}/processing-runs/` | List processing runs and status for a block | Across all images run against it |
| GET | `/api/processing-runs/{run_id}/detections/?status=new_candidate` | Review queue | Detections from one run not yet matched to an existing tree |
| POST | `/api/detections/{id}/confirm/` | Confirm a `new_candidate` detection as a real tree | Creates a `Tree` row with `source=drone_detected` |
| POST | `/api/detections/{id}/reject/` | Reject a false-positive detection | |
| POST | `/api/detections/{id}/link/` | Manually link a detection to an existing tree | Overrides an automatic match/threshold decision |
| GET | `/api/sync/pull/?since={timestamp}` | Pull server-side changes since last sync | Scoped by the requesting user's role/farm assignment |
| GET | `/api/compliance-documents/` | List compliance documents, with expiry status | See `07-compliance.md` |
| POST | `/api/harvest-batches/{id}/export-due-diligence/` | Generate a due-diligence data package | GeoJSON + structured document, see `07-compliance.md` § EUDR-specific functions |
| POST | `/api/reports/{definition_id}/run/` | Trigger on-demand report generation | Async — returns a `ReportRun` to poll/download once ready |
| GET | `/api/notifications/` | List current user's notifications | |
| PATCH | `/api/notification-preferences/` | Update which categories/channels a user receives | |

## Conventions

- Offline-writable creates accept a client-supplied UUID as the primary key; resubmitting the same UUID is idempotent, not an error.
- List endpoints scoped by role/farm assignment should filter at the queryset level (never filter in the serializer or rely on the client to only request what it should see).
- Approval and other online-only actions should return a clear error if attempted while representing a queued/offline-originated request — these are not part of the offline sync queue by design (see `05-offline-sync.md`).
