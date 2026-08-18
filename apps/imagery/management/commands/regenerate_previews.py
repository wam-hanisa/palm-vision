"""
Regenerates Image.preview_file for existing images by re-running
generate_preview() — the transparency logic in
apps/imagery/services.py's _preview_alpha_channel() only affects previews
generated from the point that logic lands forward; every image uploaded
before then has an already-generated, fully opaque preview file sitting in
storage that this command's own docstring history flagged as a known,
unresolved gap the first time transparency handling was added at all.

Safe to run repeatedly — generate_preview() always overwrites
Image.preview_file in place and never raises (failures are recorded on
Image.preview_error, same as any other run), so a bad raster among many
doesn't stop the rest.

Usage:
    python manage.py regenerate_previews            # every image
    python manage.py regenerate_previews --farm-id <uuid>
"""
from django.core.management.base import BaseCommand

from apps.imagery.models import Image
from apps.imagery.services import generate_preview


class Command(BaseCommand):
    help = "Regenerate Image.preview_file for existing images (picks up any generate_preview()/alpha-channel fix)."

    def add_arguments(self, parser):
        parser.add_argument("--farm-id", default=None, help="Only regenerate previews for this farm's images.")

    def handle(self, *args, **options):
        images = Image.objects.all()
        if options["farm_id"]:
            images = images.filter(farm_id=options["farm_id"])

        total = images.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No images match — nothing to do."))
            return

        failed = 0
        for i, image in enumerate(images.iterator(), start=1):
            generate_preview(image)
            if image.preview_error:
                failed += 1
                self.stdout.write(self.style.ERROR(f"[{i}/{total}] {image.id}: {image.preview_error}"))
            else:
                self.stdout.write(f"[{i}/{total}] {image.id}: regenerated")

        summary = f"Done: {total - failed}/{total} regenerated successfully."
        self.stdout.write(self.style.SUCCESS(summary) if failed == 0 else self.style.WARNING(summary))
