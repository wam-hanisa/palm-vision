"""
docs/04-api-spec.md rows for imagery/detection. UI rule mirrored here per
docs/06-ml-pipeline.md § UI rule mirroring the backend rule: "no global/bare
map view for reviewing imagery or triggering detection" — every endpoint
below that acts on a block (process, list runs, detections) requires the
block id in the URL, never a bare "process the most recent image" shortcut.

Scoping: imagery upload/processing is Manager/Super-Admin-level per
docs/06-ml-pipeline.md ("upload happens once back on real bandwidth, from
the office dashboard — not the field PWA"). Review-queue actions
(confirm/reject/link) are open to whoever can already act on the block —
Manager, Super Admin, or the block's assigned PIC Block — since that
mirrors who already does tree CRUD on that block.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsAssignedPicBlockOrManagerOrSuperAdmin, IsSuperAdminOrManager, user_role_on_farm
from apps.farms.models import FarmBlock, UserFarmRole
from apps.trees.models import Tree

from . import services
from .models import Image, ImageProcessingRun, ModelVersion, TreeDetection
from .serializers import (
    ImageProcessingRunSerializer,
    ImageSerializer,
    ModelVersionSerializer,
    TreeDetectionSerializer,
)
from .tasks import georeference_image_task, run_processing_task


def _user_farm_ids(user):
    return UserFarmRole.objects.filter(user=user).values_list("farm_id", flat=True)


class ImageViewSet(viewsets.ModelViewSet):
    """docs/04-api-spec.md: POST /api/farms/{farm_id}/images/ — but modeled
    as a standard viewset with `farm` in the request body rather than the
    URL, matching how the rest of this project's viewsets already work
    (see apps/operations/views.py) rather than introducing a new nested-URL
    convention just for this one resource.
    """

    serializer_class = ImageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Image.objects.select_related("farm", "uploaded_by")
        if user.is_superuser:
            return qs
        return qs.filter(farm_id__in=_user_farm_ids(user))

    def perform_create(self, serializer):
        farm = serializer.validated_data["farm"]
        user = self.request.user
        if not (user.is_superuser or user_role_on_farm(user, farm) == UserFarmRole.ROLE_MANAGER):
            raise PermissionDenied("Only a Manager or Super Admin can upload imagery for this farm.")
        image = serializer.save(uploaded_by=user)
        georeference_image_task.delay(str(image.id))

    def perform_update(self, serializer):
        farm = serializer.instance.farm
        user = self.request.user
        if not (user.is_superuser or user_role_on_farm(user, farm) == UserFarmRole.ROLE_MANAGER):
            raise PermissionDenied("Only a Manager or Super Admin can edit imagery for this farm.")
        serializer.save()


class ModelVersionViewSet(viewsets.ModelViewSet):
    """docs/06-ml-pipeline.md § Model governance. Super Admin/Manager-only
    in practice (model training/promotion isn't a PIC Block action) —
    enforced the same way as ImageViewSet, since ModelVersion has no farm
    to scope against (it's global, not per-farm).
    """

    serializer_class = ModelVersionSerializer
    queryset = ModelVersion.objects.all()
    permission_classes = [IsAuthenticated]

    def _require_manager_or_admin(self):
        user = self.request.user
        if not (user.is_superuser or UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).exists()):
            raise PermissionDenied("Only a Manager or Super Admin can manage model versions.")

    def perform_create(self, serializer):
        self._require_manager_or_admin()
        serializer.save(trained_by=self.request.user)

    def perform_update(self, serializer):
        self._require_manager_or_admin()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_manager_or_admin()
        instance.delete()


class ProcessImageView(APIView):
    """POST /api/farm-blocks/{block_id}/images/{image_id}/process/ —
    docs/04-api-spec.md: "Clips this image to this block and runs detect +
    classify; refuses if the block has no boundary polygon." That refusal
    is enforced in services.run_processing() (ProcessingRefused-equivalent
    status/error_message on the run), not duplicated here — this view's
    only job is permission + creating the run + dispatching the task.
    """

    permission_classes = [IsAuthenticated, IsSuperAdminOrManager]

    def post(self, request, block_id, image_id):
        block = get_object_or_404(FarmBlock, id=block_id)
        image = get_object_or_404(Image, id=image_id, farm_id=block.farm_id)
        self.check_object_permissions(request, block)

        if image.georeference_status != Image.GEOREFERENCE_DONE:
            raise DRFValidationError(
                {"image": "This image hasn't finished georeferencing yet — wait for georeference_status=done."}
            )

        run = ImageProcessingRun.objects.create(image=image, farm_block=block, triggered_by=request.user)

        # CELERY_TASK_ALWAYS_EAGER=True in settings.test runs this
        # synchronously during tests — see palmvision/settings/test.py.
        run_processing_task.delay(str(run.id))
        run.refresh_from_db()
        return Response(ImageProcessingRunSerializer(run).data, status=status.HTTP_201_CREATED)


class BlockProcessingRunsView(APIView):
    """GET /api/farm-blocks/{block_id}/processing-runs/ — docs/04-api-spec.md:
    "List processing runs and status for a block. Across all images run
    against it."
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, block_id):
        block = get_object_or_404(FarmBlock, id=block_id)
        user = request.user
        if not user.is_superuser:
            allowed = block.pic_id == user.id or user_role_on_farm(user, block.farm) == UserFarmRole.ROLE_MANAGER
            if not allowed:
                raise PermissionDenied("You don't have access to this block.")
        runs = ImageProcessingRun.objects.filter(farm_block=block).select_related("image")
        return Response(ImageProcessingRunSerializer(runs, many=True).data)


class ProcessingRunDetectionsView(APIView):
    """GET /api/processing-runs/{run_id}/detections/?status=new_candidate —
    docs/04-api-spec.md: "Detections from one run not yet matched to an
    existing tree." `status` filters `match_status`; omit for all.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        run = get_object_or_404(ImageProcessingRun, id=run_id)
        user = request.user
        block = run.farm_block
        if not user.is_superuser:
            allowed = block.pic_id == user.id or user_role_on_farm(user, block.farm) == UserFarmRole.ROLE_MANAGER
            if not allowed:
                raise PermissionDenied("You don't have access to this block.")

        detections = TreeDetection.objects.filter(processing_run=run).select_related("classification")
        match_status = request.query_params.get("status")
        if match_status:
            detections = detections.filter(match_status=match_status)
        return Response(TreeDetectionSerializer(detections, many=True).data)


class DetectionActionViewSet(viewsets.GenericViewSet):
    """/api/detections/{id}/confirm|reject|link/ — docs/04-api-spec.md
    review-queue actions. Not a full ModelViewSet: detections are never
    created or edited directly through this API, only acted on via these
    three named actions (docs/03-data-model.md: "every TREE_DETECTION ...
    write is a new row" — created only by the pipeline itself).
    """

    queryset = TreeDetection.objects.select_related("processing_run__farm_block__farm", "tree")
    serializer_class = TreeDetectionSerializer
    permission_classes = [IsAuthenticated, IsAssignedPicBlockOrManagerOrSuperAdmin]

    def retrieve(self, request, *args, **kwargs):
        # Not part of the API spec (detections are only reached via the
        # per-run review-queue list), but ModelViewSet-style detail-action
        # URLs need get_object() to work, which needs *some* retrieve
        # behavior registered — this just returns the detection itself.
        instance = self.get_object()
        return Response(TreeDetectionSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """docs/04-api-spec.md: "Confirm a new_candidate detection as a
        real tree — Creates a Tree row with source=drone_detected."
        """
        detection = self.get_object()
        if detection.match_status != TreeDetection.MATCH_NEW_CANDIDATE:
            raise DRFValidationError(
                {"detail": f"Only new_candidate detections can be confirmed (this one is {detection.match_status})."}
            )
        tree = services.confirm_new_candidate(detection, request.user)
        detection.refresh_from_db()
        return Response(
            {"detection": TreeDetectionSerializer(detection).data, "tree_id": str(tree.id)},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """docs/04-api-spec.md: "Reject a false-positive detection." """
        detection = self.get_object()
        detection.match_status = TreeDetection.MATCH_REJECTED
        detection.save(update_fields=["match_status"])
        return Response(TreeDetectionSerializer(detection).data)

    @action(detail=True, methods=["post"])
    def link(self, request, pk=None):
        """docs/04-api-spec.md: "Manually link a detection to an existing
        tree — Overrides an automatic match/threshold decision." Used for
        both ambiguous detections (human picks the right one of several
        candidates) and correcting a wrong automatic match.
        """
        detection = self.get_object()
        tree_id = request.data.get("tree_id")
        if not tree_id:
            raise DRFValidationError({"tree_id": "Required."})
        tree = get_object_or_404(Tree, id=tree_id, farm_block=detection.processing_run.farm_block)
        detection.tree = tree
        detection.match_status = TreeDetection.MATCH_MATCHED
        detection.save(update_fields=["tree", "match_status"])
        return Response(TreeDetectionSerializer(detection).data)
