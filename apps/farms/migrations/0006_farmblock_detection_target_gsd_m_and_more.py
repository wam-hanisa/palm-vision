from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('farms', '0005_farmblock_crown_detection_iou_threshold_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='farmblock',
            name='detection_target_gsd_m',
            field=models.FloatField(blank=True, help_text="Overrides DETECTION_DEFAULT_TARGET_GSD_M for this block — the ground sample distance (meters/pixel) apps/imagery/ml/detection.py's _load_real_detector() resamples the orthomosaic to before tiling, so a real crown appears roughly the pixel size the detector was trained to recognize. Found necessary after a real report: this project's own imagery (1.843cm/px) is 2-5x finer than the training data's implied GSD, causing the detector to fire on individual fronds instead of whole crowns. Per-block since actual crown size (young vs. mature palms, different commodities) genuinely varies.", null=True),
        ),
        migrations.AddField(
            model_name='historicalfarmblock',
            name='detection_target_gsd_m',
            field=models.FloatField(blank=True, help_text="Overrides DETECTION_DEFAULT_TARGET_GSD_M for this block — the ground sample distance (meters/pixel) apps/imagery/ml/detection.py's _load_real_detector() resamples the orthomosaic to before tiling, so a real crown appears roughly the pixel size the detector was trained to recognize. Found necessary after a real report: this project's own imagery (1.843cm/px) is 2-5x finer than the training data's implied GSD, causing the detector to fire on individual fronds instead of whole crowns. Per-block since actual crown size (young vs. mature palms, different commodities) genuinely varies.", null=True),
        ),
    ]
