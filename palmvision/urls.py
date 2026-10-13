from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.static import serve as static_serve
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/", include("apps.farms.urls")),
    path("api/", include("apps.trees.urls")),
    path("api/", include("apps.operations.urls")),
    path("api/", include("apps.sync.urls")),
    path("api/", include("apps.imagery.urls")),
    path("api/", include("apps.compliance.urls")),
    path("api/", include("apps.reporting.urls")),
    path("api/", include("apps.notifications.urls")),
    path("dashboard/", include("apps.dashboard.urls")),
]

if settings.DEBUG:
    # Local-dev-only media serving — a real deployment serves this from
    # S3/nginx instead, same reasoning as the PWA static serving below.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# --- PWA field client (Phase 4) ---
# Served directly via django.views.static.serve rather than STATICFILES_DIRS
# + collectstatic — this is deliberately the "runs locally" path (CLAUDE.md's
# local-first rule), not a production static-asset pipeline. A real
# deployment should serve pwa/ from nginx/whitenoise/a CDN instead; that's
# out of scope until every build-plan phase is verified locally (see
# docs/02-build-plan.md's deployment note).
#
# service-worker.js and manifest.webmanifest are served from the site root
# (not under /pwa/) specifically so the service worker's default scope
# covers the whole origin — see pwa/service-worker.js's docstring for why
# that's safe (it only ever handles the field app's own files).


def _pwa_serve(request, **kwargs):
    # Without Cache-Control, browsers apply heuristic freshness (~10% of the
    # time since Last-Modified) and keep running an old app.js/sync.js for
    # hours after a change. no-cache still lets them reuse their copy, but
    # only after a revalidation (a cheap 304 when nothing changed).
    response = static_serve(request, **kwargs)
    response["Cache-Control"] = "no-cache"
    return response


urlpatterns += [
    path("field/", _pwa_serve, {"document_root": settings.BASE_DIR / "pwa", "path": "index.html"}, name="pwa-shell"),
    path("service-worker.js", _pwa_serve, {"document_root": settings.BASE_DIR / "pwa", "path": "service-worker.js"}),
    path("manifest.webmanifest", _pwa_serve, {"document_root": settings.BASE_DIR / "pwa", "path": "manifest.webmanifest"}),
    path("pwa/<path:path>", _pwa_serve, {"document_root": settings.BASE_DIR / "pwa"}),
]
