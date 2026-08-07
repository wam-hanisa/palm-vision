# Reporting module

The original app's `/report` route was never built out — this is a real design, aimed at what a Manager or Super Admin needs to pull out of the system.

## Report types

- **Production report** — FFB quantity/weight by block, farm, or period, planned vs. actual, trend over time. Rolls up from tree-level `HarvestTree` records or block-level `ActualHarvest` totals depending on which granularity a given harvest event was logged at (`05-offline-sync.md`).
- **Input report** — fertilizer types and quantities applied, by block and period.
- **Tree health report** — healthy/small/yellow counts and trend per block, pulled from `TreeHealthClassification` history (`03-data-model.md`), not just a current snapshot.
- **Compliance report** — document expiry status, audit results, deforestation-baseline status per block — built for handing to an auditor or certifier (`07-compliance.md`).
- **Model performance report** — the view from `06-ml-pipeline.md` § Model governance, formalized as a periodic export.

## Generation and delivery

- `ReportDefinition` (report type + filters/parameters + optional recurrence schedule) and `ReportRun` (one generated instance — file, generated_at, status), following the same async-job pattern as `ImageProcessingRun`. Report generation runs as a Celery task — some of these (a full-year production report across many blocks) aren't fast enough to generate synchronously in a request.
- **On-demand**: triggered from the dashboard, generates and returns a downloadable file.
- **Scheduled**: Celery Beat periodic tasks (e.g. monthly production report) that generate and email the result automatically to whoever's subscribed.
- **Export formats**: PDF (WeasyPrint) for anything meant to be shared/filed as-is; Excel (`openpyxl`) for anything a Manager will want to filter/pivot.
- **Access control**: report types touching compliance or financial-adjacent data are restricted the same way the underlying records are — a report is just another view of the data, not a side door around the permission model.
