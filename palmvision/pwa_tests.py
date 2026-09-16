"""Verifies the PWA shell is actually served at the URLs pwa/index.html and
pwa/service-worker.js assume — docs/02-build-plan.md Phase 4 depends on
these being reachable before any browser-side offline testing is possible.
These are deliberately public (no auth) since the app shell has to load
before login can happen.
"""
import pytest
from django.test import Client


def _read(response):
    """django.views.static.serve returns a FileResponse, which streams its
    body rather than buffering it in .content (that's what it's for — real
    static-file serving shouldn't load the whole file into memory). Tests
    need the actual bytes, so drain streaming_content instead.
    """
    return b"".join(response.streaming_content)


@pytest.mark.django_db
def test_pwa_shell_served_at_field():
    response = Client().get("/field/")
    assert response.status_code == 200
    body = _read(response).decode()
    assert "Palm Vision" in body
    assert '/pwa/js/app.js' in body


def test_service_worker_served_at_root_for_full_scope():
    # Deliberately NOT under /pwa/ — see palmvision/urls.py comment on why
    # scope requires this exact path.
    response = Client().get("/service-worker.js")
    assert response.status_code == 200
    assert b"palmvision-shell" in _read(response)


def test_manifest_served_at_root():
    import json

    response = Client().get("/manifest.webmanifest")
    assert response.status_code == 200
    # Content-type mapping for .webmanifest varies by OS/Python mimetypes
    # registry — what actually matters is that it's valid, parseable JSON.
    manifest = json.loads(_read(response))
    assert manifest["start_url"] == "/field/"


@pytest.mark.parametrize("path", ["db.js", "api.js", "sync.js", "geo.js", "app.js"])
def test_pwa_js_files_served(path):
    response = Client().get(f"/pwa/js/{path}")
    assert response.status_code == 200


def test_pwa_css_served():
    response = Client().get("/pwa/css/app.css")
    assert response.status_code == 200


def test_pwa_icons_served():
    for size in ("192", "512"):
        response = Client().get(f"/pwa/icons/icon-{size}.png")
        assert response.status_code == 200


def test_vendored_webfont_is_served():
    """docs/09-design-system.md asks for a humanist sans (Inter/Public Sans).
    The font is vendored into pwa/fonts/ rather than linked from the Google
    Fonts CDN, because an external stylesheet would fall back to a system
    font in exactly the offline case this PWA exists for. If this route
    breaks, that fallback happens silently — nothing else would catch it.
    """
    response = Client().get("/pwa/fonts/public-sans-latin.woff2")
    assert response.status_code == 200
    body = b"".join(response.streaming_content)
    # woff2 files start with the signature 'wOF2'.
    assert body[:4] == b"wOF2"


def test_service_worker_precaches_the_webfont():
    """A font that is served but not precached still fails offline."""
    response = Client().get("/service-worker.js")
    body = b"".join(response.streaming_content).decode()
    assert "/pwa/fonts/public-sans-latin.woff2" in body
