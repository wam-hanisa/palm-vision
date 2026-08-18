from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('imagery', '0005_treehealthclassification_patch_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='imageprocessingrun',
            name='progress_current',
            field=models.PositiveIntegerField(default=0, help_text="Reused across whichever stage is currently running — orthomosaic tiles processed so far during 'detecting' (see apps/imagery/ml/detection.py's on_tile_progress callback), or detections classified so far during 'classifying'. Meaningless outside those two statuses; see progress_percent."),
        ),
        migrations.AddField(
            model_name='imageprocessingrun',
            name='progress_total',
            field=models.PositiveIntegerField(default=0, help_text='The denominator progress_current counts up to for the current stage — see progress_current.'),
        ),
    ]
