"""
Backfills Tree.current_health from each tree's most recent
TreeHealthClassification.

Reported directly, via a screenshot showing every tree on the Farm Map
rendered gray (healthColors.unknown) regardless of its real health. Root
cause: Tree.current_health was never actually written from a real
classification result anywhere in the pipeline until
classify_detection()/confirm_new_candidate() were fixed to do so (see
BUILD_STATUS.md's matching update) — every tree confirmed/matched before
that fix landed is stuck at the model default ("unknown") no matter what
the classifier actually said. This command is the one-time catch-up for
trees that already exist; the code fix alone only covers trees
matched/confirmed going forward.

Safe to run repeatedly — idempotent, recomputes and overwrites
current_health from whatever TreeHealthClassification history already
exists; never touches TreeDetection/TreeHealthClassification rows
themselves.

Usage:
    python manage.py sync_tree_health              # every tree
    python manage.py sync_tree_health --block-id <uuid>
"""
from django.core.management.base import BaseCommand

from apps.imagery.models import TreeHealthClassification
from apps.trees.models import Tree


class Command(BaseCommand):
    help = "Backfill Tree.current_health from each tree's most recent TreeHealthClassification."

    def add_arguments(self, parser):
        parser.add_argument("--block-id", default=None, help="Only sync trees in this block.")

    def handle(self, *args, **options):
        trees = Tree.objects.all()
        if options["block_id"]:
            trees = trees.filter(farm_block_id=options["block_id"])

        total = trees.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No trees match — nothing to do."))
            return

        updated = 0
        unclassified = 0
        for i, tree in enumerate(trees.iterator(), start=1):
            # A tree can have several TreeDetection rows over time (a new
            # row per processing run — see TreeDetection's own docstring);
            # the most recently created one's classification is what
            # "current" health should mean.
            classification = (
                TreeHealthClassification.objects.filter(tree_detection__tree=tree)
                .order_by("-tree_detection__created_at")
                .first()
            )
            if classification is None:
                unclassified += 1
                continue
            new_health = classification.effective_label
            if tree.current_health != new_health:
                tree.current_health = new_health
                tree.save(update_fields=["current_health"])
                updated += 1
                self.stdout.write(f"[{i}/{total}] {tree.id}: -> {new_health}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {updated}/{total} trees updated, {unclassified} had no classification yet."
            )
        )
