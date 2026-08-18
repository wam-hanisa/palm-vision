from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('farms', '0004_farmblock_fertilizer_interval_days_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='farmblock',
            name='crown_detection_iou_threshold',
            field=models.FloatField(blank=True, help_text="Overrides CROWN_DETECTION_DEFAULT_IOU_THRESHOLD for this block — how much two crown-detection boxes from adjacent orthomosaic tiles must overlap before apps/imagery/ml/detection.py's _world_nms() treats them as the same tree rather than two separate ones. Same per-block-override reasoning as tree_match_threshold_m above: planting density (and therefore real crown size relative to the tiling grid) genuinely differs by block/commodity, so one global constant won't fit every block equally well.", null=True),
        ),
        migrations.AddField(
            model_name='historicalfarmblock',
            name='crown_detection_iou_threshold',
            field=models.FloatField(blank=True, help_text="Overrides CROWN_DETECTION_DEFAULT_IOU_THRESHOLD for this block — how much two crown-detection boxes from adjacent orthomosaic tiles must overlap before apps/imagery/ml/detection.py's _world_nms() treats them as the same tree rather than two separate ones. Same per-block-override reasoning as tree_match_threshold_m above: planting density (and therefore real crown size relative to the tiling grid) genuinely differs by block/commodity, so one global constant won't fit every block equally well.", null=True),
        ),
    ]
