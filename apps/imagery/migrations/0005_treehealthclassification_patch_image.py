from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('imagery', '0004_image_cog_error_image_is_cog'),
    ]

    operations = [
        migrations.AddField(
            model_name='treehealthclassification',
            name='patch_image',
            field=models.ImageField(blank=True, help_text="The exact cropped patch classify_detection() fed to the classifier, saved at classification time so the review queue can show what the model actually saw — see apps/imagery/services.py classify_detection(). Never regenerated on read: the source Image may later move to cold storage or be deleted (see Image delete), but this patch shouldn't disappear along with it. Null only when the patch itself was unusable (crop_patch() returned None, e.g. a detection too close to the clipped raster's edge) — same case that already forces health_label to the review-routing default rather than a real prediction.", null=True, upload_to='imagery/classification_patches/%Y/%m/'),
        ),
    ]
