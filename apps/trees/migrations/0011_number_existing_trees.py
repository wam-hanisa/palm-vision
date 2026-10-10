"""Numbers every existing tree within its block, by position.

Order is a "snake" walk along the block's planting rows: row by row,
alternating direction, so neighbouring trees — including the end of one
row and the start of the next — get nearby numbers, the way someone walks
a block.

Rows aren't necessarily east–west, so the row direction is found per
block: every candidate angle (3° steps) and row width is tried, and the
one whose walk has the shortest typical step between consecutive trees
wins. On the real B4/B5 data this brings the median step to ~9 m (one
tree spacing) versus ~15 m for plain east–west bands.

New trees are numbered on save (Tree.save()) with the block's next free
number, so this only ever needs to run once.
"""
import math
import statistics
from collections import defaultdict

from django.db import migrations
from django.utils import timezone

METRES_PER_DEGREE = 111_320
ANGLES = range(0, 180, 3)
ROW_WIDTHS_M = (7, 8, 9, 10)


def _snake_order(points, angle_deg, row_width_m):
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    rows = defaultdict(list)
    for i, (x, y) in enumerate(points):
        along = x * cos_a + y * sin_a
        across = -x * sin_a + y * cos_a
        rows[round(-across / row_width_m)].append((along, i))
    order = []
    for row_index, key in enumerate(sorted(rows)):
        order.extend(i for _, i in sorted(rows[key], reverse=bool(row_index % 2)))
    return order


def _walk_score(points, order):
    steps = sorted(math.dist(points[a], points[b]) for a, b in zip(order, order[1:]))
    return (statistics.median(steps), steps[int(len(steps) * 0.9)])


def _best_order(trees):
    if len(trees) < 3:
        return list(range(len(trees)))
    lat0 = statistics.mean(t.location.y for t in trees)
    k = math.cos(math.radians(lat0))
    points = [(t.location.x * METRES_PER_DEGREE * k, t.location.y * METRES_PER_DEGREE) for t in trees]
    candidates = (_snake_order(points, angle, width) for angle in ANGLES for width in ROW_WIDTHS_M)
    return min(candidates, key=lambda order: _walk_score(points, order))


def number_existing_trees(apps, schema_editor):
    Tree = apps.get_model("trees", "Tree")
    by_block = defaultdict(list)
    for tree in Tree.objects.filter(number__isnull=True).only("id", "farm_block_id", "location"):
        by_block[tree.farm_block_id].append(tree)

    # bulk_update() skips auto_now, so updated_at is set explicitly: the
    # field app's incremental sync only re-fetches trees whose updated_at
    # moved, and without this no device would ever receive the numbers.
    now = timezone.now()
    for trees in by_block.values():
        ordered = [trees[i] for i in _best_order(trees)]
        for number, tree in enumerate(ordered, start=1):
            tree.number = number
            tree.updated_at = now
        Tree.objects.bulk_update(ordered, ["number", "updated_at"], batch_size=500)


def clear_numbers(apps, schema_editor):
    apps.get_model("trees", "Tree").objects.update(number=None)


class Migration(migrations.Migration):
    dependencies = [
        ("trees", "0010_tree_number"),
    ]

    operations = [
        migrations.RunPython(number_existing_trees, clear_numbers),
    ]
