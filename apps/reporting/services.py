"""
Report data-building + rendering. Deliberately split into three stages, like
apps/imagery/services.py's pipeline functions — each independently testable:

1. `_resolve_scope()` — turn a ReportDefinition's `filters` JSON into a
   concrete (farm, blocks, date_from, date_to).
2. `_build_production_data()` / `_build_tree_health_data()` — plain-Python
   data assembly, no rendering concerns. Only these two are implemented —
   see models.py's docstring for why input/compliance/model_performance
   report types raise NotImplementedError for now.
3. `_render_pdf()` / `_render_xlsx()` — take that data and produce bytes.
   WeasyPrint and openpyxl are imported at call time, not module load time —
   matching the lazy-import pattern apps/imagery/ml/*.py uses for
   torch/ultralytics, so `manage.py check`/test collection never depends on
   these being importable in every environment this code might run in.

`generate_report()` ties the three stages together and is what
apps/reporting/tasks.py's Celery task actually calls.
"""
import io
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db.models import Sum
from django.utils import timezone

from apps.farms.models import FarmBlock
from apps.imagery.models import TreeHealthClassification
from apps.operations.models import ActualHarvest, Harvest

from .models import ReportDefinition, ReportRun


class ReportGenerationError(Exception):
    """Wraps any failure during generation so ReportRun.error_message always
    gets a clear, single-line cause rather than a raw traceback.
    """


def _resolve_scope(definition: ReportDefinition):
    filters = definition.filters or {}
    farm = definition.farm

    block_id = filters.get("farm_block_id")
    blocks = FarmBlock.objects.filter(farm=farm)
    if block_id:
        blocks = blocks.filter(id=block_id)

    date_to = filters.get("date_to")
    date_from = filters.get("date_from")
    today = timezone.localdate()
    date_to = date_to or today.isoformat()
    date_from = date_from or (today - timedelta(days=30)).isoformat()

    return farm, list(blocks), date_from, date_to


def _build_production_data(definition: ReportDefinition):
    """docs/08-reporting.md: 'FFB quantity/weight by block, farm, or period,
    planned vs. actual, trend over time. Rolls up from tree-level
    HarvestTree records or block-level ActualHarvest totals depending on
    which granularity a given harvest event was logged at.' The per-block
    'actual' figure here is exactly ActualHarvest.get_effective_quantity()
    summed — that method already encodes the tree-level-overrides-manual
    rule (docs/05-offline-sync.md), so this doesn't re-derive it.
    """
    farm, blocks, date_from, date_to = _resolve_scope(definition)
    rows = []
    for block in blocks:
        planned = (
            Harvest.objects.filter(farm_block=block, assessment_date__range=[date_from, date_to])
            .aggregate(total=Sum("assessment_quantity"))["total"]
            or 0.0
        )
        actual_total = 0.0
        actuals = ActualHarvest.objects.filter(
            harvest__farm_block=block, date__range=[date_from, date_to]
        ).prefetch_related("harvest_trees")
        for actual in actuals:
            actual_total += actual.get_effective_quantity() or 0.0
        rows.append({"block_name": block.name, "planned_quantity": planned, "actual_quantity": actual_total})

    return {
        "farm_name": farm.name,
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "total_planned": sum(r["planned_quantity"] for r in rows),
        "total_actual": sum(r["actual_quantity"] for r in rows),
    }


def _build_tree_health_data(definition: ReportDefinition):
    """docs/08-reporting.md: 'healthy/small/yellow counts and trend per
    block, pulled from TreeHealthClassification history, not just a
    current snapshot.' Uses `effective_label` (human correction if one
    exists, otherwise the model's prediction) per
    apps/imagery/models.py — a reviewed correction should count as the
    real answer in a report, not the original (possibly wrong) prediction.
    """
    farm, blocks, date_from, date_to = _resolve_scope(definition)
    rows = []
    for block in blocks:
        classifications = TreeHealthClassification.objects.filter(
            tree_detection__processing_run__farm_block=block,
            created_at__date__range=[date_from, date_to],
        )
        counts = {label: 0 for label, _ in TreeHealthClassification.HEALTH_CHOICES}
        for classification in classifications:
            label = classification.effective_label
            counts[label] = counts.get(label, 0) + 1
        rows.append({"block_name": block.name, **counts, "total": sum(counts.values())})

    return {
        "farm_name": farm.name,
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "health_labels": [label for label, _ in TreeHealthClassification.HEALTH_CHOICES],
    }


_DATA_BUILDERS = {
    ReportDefinition.TYPE_PRODUCTION: _build_production_data,
    ReportDefinition.TYPE_TREE_HEALTH: _build_tree_health_data,
}


def _render_pdf(definition: ReportDefinition, data: dict) -> bytes:
    from weasyprint import HTML

    if definition.report_type == ReportDefinition.TYPE_PRODUCTION:
        rows_html = "".join(
            f"<tr><td>{r['block_name']}</td><td>{r['planned_quantity']:.1f}</td><td>{r['actual_quantity']:.1f}</td></tr>"
            for r in data["rows"]
        )
        body = f"""
        <h1>Production report — {data['farm_name']}</h1>
        <p>{data['date_from']} to {data['date_to']}</p>
        <table border="1" cellpadding="4" cellspacing="0">
          <tr><th>Block</th><th>Planned</th><th>Actual</th></tr>
          {rows_html}
          <tr><th>Total</th><th>{data['total_planned']:.1f}</th><th>{data['total_actual']:.1f}</th></tr>
        </table>
        """
    else:  # tree_health
        labels = data["health_labels"]
        header = "".join(f"<th>{label}</th>" for label in labels)
        rows_html = "".join(
            "<tr><td>{}</td>{}<td>{}</td></tr>".format(
                r["block_name"], "".join(f"<td>{r[label]}</td>" for label in labels), r["total"]
            )
            for r in data["rows"]
        )
        body = f"""
        <h1>Tree health report — {data['farm_name']}</h1>
        <p>{data['date_from']} to {data['date_to']}</p>
        <table border="1" cellpadding="4" cellspacing="0">
          <tr><th>Block</th>{header}<th>Total</th></tr>
          {rows_html}
        </table>
        """

    return HTML(string=f"<html><body>{body}</body></html>").write_pdf()


def _render_xlsx(definition: ReportDefinition, data: dict) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active

    if definition.report_type == ReportDefinition.TYPE_PRODUCTION:
        ws.title = "Production"
        ws.append(["Block", "Planned", "Actual"])
        for r in data["rows"]:
            ws.append([r["block_name"], r["planned_quantity"], r["actual_quantity"]])
        ws.append(["Total", data["total_planned"], data["total_actual"]])
    else:  # tree_health
        ws.title = "Tree health"
        labels = data["health_labels"]
        ws.append(["Block", *[label.capitalize() for label in labels], "Total"])
        for r in data["rows"]:
            ws.append([r["block_name"], *[r[label] for label in labels], r["total"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def generate_report(run: ReportRun) -> ReportRun:
    definition = run.definition
    run.status = ReportRun.STATUS_RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    try:
        builder = _DATA_BUILDERS.get(definition.report_type)
        if builder is None:
            raise ReportGenerationError(
                f"Report type '{definition.report_type}' isn't implemented yet — "
                "only production and tree_health are built in Phase 7. "
                "See apps/reporting/models.py's docstring."
            )
        data = builder(definition)

        if definition.export_format == ReportDefinition.FORMAT_PDF:
            file_bytes = _render_pdf(definition, data)
            extension = "pdf"
        else:
            file_bytes = _render_xlsx(definition, data)
            extension = "xlsx"

        filename = f"{definition.report_type}_{run.id}.{extension}"
        run.file.save(filename, ContentFile(file_bytes), save=False)
        run.status = ReportRun.STATUS_READY
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "file", "completed_at"])
    except Exception as exc:  # noqa: BLE001 — any failure here must be recorded on the run, not crash the Celery task
        run.status = ReportRun.STATUS_FAILED
        run.error_message = str(exc)
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at"])

    return run
