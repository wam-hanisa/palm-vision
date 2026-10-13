import uuid

from django.db import migrations


def give_each_existing_row_its_own_batch_id(apps, schema_editor):
    """The previous migration added `batch_id` with a callable
    (`uuid.uuid4`) default. Django's AddField only calls a callable
    default ONCE and uses that single value to backfill every pre-existing
    row (a documented Django migration limitation, not a bug in the field
    itself) — so every TreeCareRecord that existed before this feature
    landed with the exact same batch_id, which would make
    BlockCareHistoryView (apps/dashboard/views.py) wrongly merge unrelated
    historical marking actions — from different trees, different dates —
    into one history entry.

    Fixes it by giving each pre-existing row its own fresh, unique
    batch_id — exactly equivalent to how it always displayed before this
    feature existed (one entry per record), and a safe/correct default
    since there's no way to know which historical rows were genuinely part
    of the same original bulk submission. A no-op on a fresh database with
    no existing rows.
    """
    TreeCareRecord = apps.get_model("trees", "TreeCareRecord")
    for record in TreeCareRecord.objects.all().only("id"):
        TreeCareRecord.objects.filter(pk=record.pk).update(batch_id=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ("trees", "0007_treecarerecord_action_treecarerecord_batch_id_and_more"),
    ]

    operations = [
        migrations.RunPython(give_each_existing_row_its_own_batch_id, migrations.RunPython.noop),
    ]
