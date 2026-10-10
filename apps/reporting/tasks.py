"""Thin Celery wrappers, same split as apps/imagery/tasks.py — no report
logic lives here, only orchestration + delivery.
"""
from celery import shared_task
from django.core.mail import EmailMessage

from . import services
from .models import ReportDefinition, ReportRun


@shared_task
def generate_report_task(run_id):
    run = ReportRun.objects.get(id=run_id)
    services.generate_report(run)
    if run.status == ReportRun.STATUS_READY and run.definition.recurrence != ReportDefinition.RECURRENCE_NONE:
        _email_subscribers(run)


def _email_subscribers(run: ReportRun):
    """docs/08-reporting.md: 'generate and email the result automatically to
    whoever's subscribed.' Only called for scheduled runs — an on-demand
    run is downloaded directly by the user who requested it, not emailed.
    Uses whatever EMAIL_BACKEND is configured (console backend for local
    dev — settings/base.py — so 'sent' mail shows up in the worker's logs,
    not a real inbox, until SMTP is configured for a real deployment).
    """
    eligible = services.eligible_subscribers(run.definition.farm)
    subscribers = run.definition.subscribers.filter(id__in=eligible.values("id")).exclude(email="")
    if not subscribers.exists() or not run.file:
        return
    message = EmailMessage(
        subject=f"Palm Vision report: {run.definition.name}",
        body=f"Your scheduled report '{run.definition.name}' is attached.",
        to=list(subscribers.values_list("email", flat=True)),
    )
    run.file.open("rb")
    try:
        message.attach(run.file.name.split("/")[-1], run.file.read())
    finally:
        run.file.close()
    message.send(fail_silently=True)


@shared_task
def run_scheduled_reports():
    """Celery Beat periodic task — docs/08-reporting.md 'Scheduled: Celery
    Beat periodic tasks ... that generate and email the result
    automatically.' Runs daily (see CELERY_BEAT_SCHEDULE in
    palmvision/settings/base.py); a definition with recurrence=daily fires
    every day this runs, weekly fires only when today is Monday, monthly
    only on the 1st — deliberately simple fixed-day rules rather than a
    full cron-per-definition scheduler, which docs/08-reporting.md doesn't
    ask for.
    """
    from django.utils import timezone

    today = timezone.localdate()
    due = [ReportDefinition.RECURRENCE_DAILY]
    if today.weekday() == 0:  # Monday
        due.append(ReportDefinition.RECURRENCE_WEEKLY)
    if today.day == 1:
        due.append(ReportDefinition.RECURRENCE_MONTHLY)

    for definition in ReportDefinition.objects.filter(recurrence__in=due):
        run = ReportRun.objects.create(definition=definition, triggered_by=None)
        generate_report_task.delay(str(run.id))
