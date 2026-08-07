"""JWT auth smoke tests — docs/02-build-plan.md Phase 3 verification gate."""
import pytest
from rest_framework import status
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_obtain_token_with_valid_credentials(manager):
    client = APIClient()
    response = client.post("/api/token/", {"username": "manager", "password": "manager12345"})
    assert response.status_code == status.HTTP_200_OK
    assert "access" in response.data
    assert "refresh" in response.data


@pytest.mark.django_db
def test_obtain_token_with_wrong_password_is_rejected(manager):
    client = APIClient()
    response = client.post("/api/token/", {"username": "manager", "password": "wrong-password"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
def test_refresh_token_issues_new_access_token(manager):
    client = APIClient()
    obtain = client.post("/api/token/", {"username": "manager", "password": "manager12345"})
    response = client.post("/api/token/refresh/", {"refresh": obtain.data["refresh"]})
    assert response.status_code == status.HTTP_200_OK
    assert "access" in response.data


@pytest.mark.django_db
def test_unauthenticated_request_is_rejected(api_client):
    response = api_client.get("/api/farms/")
    # DRF returns 401 if any configured authenticator advertises a
    # WWW-Authenticate scheme, otherwise 403 — both mean "not authenticated,"
    # so accept either rather than pinning to whichever simplejwt version
    # happens to implement authenticate_header().
    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
