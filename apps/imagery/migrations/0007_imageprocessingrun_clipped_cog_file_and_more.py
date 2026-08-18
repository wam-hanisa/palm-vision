import django.contrib.gis.db.models.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('imagery', '0006_imageprocessingrun_progress_current_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='imageprocessingrun',
            name='clipped_cog_file',
            field=models.FileField(blank=True, help_text="image.raw_file physically clipped to farm_block.boundary and re-encoded as a standalone Cloud Optimized GeoTIFF — see apps/imagery/services.py's persist_clipped_block_imagery(). Distinct from clip_to_block()'s own in-memory clip (used transiently for detection/classification and then discarded): this is a real, reusable file, generated so a real orthomosaic that spans several blocks can be displayed/re-processed one block at a time without ever showing (or re-reading) ground that belongs to a different block.", null=True, upload_to='imagery/clipped/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='imageprocessingrun',
            name='clipped_preview_file',
            field=models.ImageField(blank=True, help_text='Downsampled web-viewable PNG rendered from clipped_cog_file — the block-scoped counterpart to Image.preview_file (which always shows the WHOLE uploaded file’s footprint). This is what the Farm Map actually overlays for a selected block, so a block’s imagery view no longer shows real pixels belonging to a neighboring block.', null=True, upload_to='imagery/clipped_previews/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='imageprocessingrun',
            name='clipped_footprint',
            field=django.contrib.gis.db.models.fields.PolygonField(blank=True, help_text='The real geographic extent of clipped_cog_file/clipped_preview_file — farm_block.boundary intersected with image.footprint (an image doesn’t always fully cover the block it’s being processed against), reprojected to 4326. Used as the Leaflet overlay bounds for clipped_preview_file — using farm_block.boundary’s own (rectangular bounding box of the) extent instead would stretch the image over ground the clip never actually covered.', null=True, srid=4326),
        ),
        migrations.AddField(
            model_name='imageprocessingrun',
            name='clip_persist_error',
            field=models.TextField(blank=True, help_text="Set if persist_clipped_block_imagery() failed — never blocks detection/classification, which use clip_to_block()'s own in-memory clip directly and don't depend on this at all."),
        ),
    ]
