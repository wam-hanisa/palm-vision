"""Finds (and with --delete removes) imagery files no database row points to.

Found in the imagery audit: ~11.7 GB of a 20.5 GB imagery folder was
orphaned — clipped files of runs whose image was deleted, ".cogswap"
leftovers from interrupted COG conversions, and raw uploads with no Image.
Dry run by default. Files modified within --min-age-hours are always kept,
so an upload or conversion in progress is never touched.
"""
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.imagery.models import Image, ImageProcessingRun, TreeHealthClassification

SUBDIRS = ("imagery/raw", "imagery/clipped", "imagery/clipped_previews", "imagery/previews", "imagery/classification_patches")


def referenced_names():
    names = set()
    for image in Image.objects.all().only("raw_file", "preview_file"):
        names.update(f.name for f in (image.raw_file, image.preview_file) if f)
    for run in ImageProcessingRun.objects.all().only("clipped_cog_file", "clipped_preview_file"):
        names.update(f.name for f in (run.clipped_cog_file, run.clipped_preview_file) if f)
    names.update(c.patch_image.name for c in TreeHealthClassification.objects.exclude(patch_image="").only("patch_image"))
    return names


def find_orphans(min_age_hours=6):
    root = str(settings.MEDIA_ROOT)
    referenced = referenced_names()
    cutoff = time.time() - min_age_hours * 3600
    orphans = []
    for sub in SUBDIRS:
        for directory, _, files in os.walk(os.path.join(root, sub)):
            for filename in files:
                path = os.path.join(directory, filename)
                name = os.path.relpath(path, root).replace(os.sep, "/")
                if name not in referenced and os.path.getmtime(path) < cutoff:
                    orphans.append((name, path, os.path.getsize(path)))
    return orphans


class Command(BaseCommand):
    help = "List (default) or delete (--delete) imagery files that no database row references."

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true", help="Actually delete the files (default: dry run).")
        parser.add_argument("--min-age-hours", type=float, default=6, help="Keep files modified more recently than this.")

    def handle(self, *args, **options):
        orphans = find_orphans(options["min_age_hours"])
        total = sum(size for _, _, size in orphans)
        for name, _, size in sorted(orphans, key=lambda o: -o[2]):
            self.stdout.write(f"{size / 1e6:9.1f} MB  {name}")
        verb = "Deleted" if options["delete"] else "Would delete (dry run — pass --delete)"
        if options["delete"]:
            for _, path, _ in orphans:
                os.remove(path)
        self.stdout.write(self.style.SUCCESS(f"{verb}: {len(orphans)} files, {total / 1e9:.2f} GB"))
