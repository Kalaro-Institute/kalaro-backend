import pytest
from django.core import signing
from django.test import Client

from apps.accounts.models import Role, User
from apps.live.google_calendar import make_state
from apps.live.models import GoogleCalendarGrant

pytestmark = pytest.mark.django_db
URL = "/api/v1/live/google/callback"


@pytest.fixture
def instructor(settings):
    settings.FRONTEND_BASE_URL = "https://app.example.com"
    return User.objects.create_user(email="i@test.com", password="supersecret123",
                                    role=Role.INSTRUCTOR, email_verified=True)


def test_success_saves_token_and_returns_to_frontend(instructor, monkeypatch):
    monkeypatch.setattr("apps.live.api.exchange_code_for_refresh_token", lambda code: "rt-1")
    r = Client().get(URL, {"code": "abc", "state": make_state(instructor.id)})
    assert r.status_code == 302
    assert r["Location"] == "https://app.example.com/dashboard/instructor/live-classes?google_connected=true"
    assert GoogleCalendarGrant.objects.get(user=instructor).refresh_token == "rt-1"


def test_reconnect_updates_existing_grant(instructor, monkeypatch):
    GoogleCalendarGrant.objects.create(user=instructor, refresh_token="old")
    monkeypatch.setattr("apps.live.api.exchange_code_for_refresh_token", lambda code: "new")
    Client().get(URL, {"code": "abc", "state": make_state(instructor.id)})
    assert GoogleCalendarGrant.objects.get(user=instructor).refresh_token == "new"


@pytest.mark.parametrize("params", [
    {"error": "access_denied"},                   # user clicked Cancel
    {"code": "abc"},                              # no state
    {"code": "abc", "state": "garbage"},          # tampered / truncated state
])
def test_bad_requests_go_back_with_error_not_500(instructor, params):
    r = Client().get(URL, params)
    assert r.status_code == 302 and r["Location"].endswith("?google_connected=false")
    assert not GoogleCalendarGrant.objects.exists()


def test_expired_state_rejected(instructor, monkeypatch):
    state = make_state(instructor.id)
    monkeypatch.setattr("apps.live.google_calendar.signing.loads",
                        lambda *a, **k: (_ for _ in ()).throw(signing.SignatureExpired("old")))
    r = Client().get(URL, {"code": "abc", "state": state})
    assert r["Location"].endswith("?google_connected=false")


def test_google_exchange_failure_goes_back_with_error(instructor, monkeypatch):
    def boom(code):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr("apps.live.api.exchange_code_for_refresh_token", boom)
    r = Client().get(URL, {"code": "abc", "state": make_state(instructor.id)})
    assert r["Location"].endswith("?google_connected=false")
    assert not GoogleCalendarGrant.objects.exists()


def test_missing_refresh_token_is_an_error(instructor, monkeypatch):
    monkeypatch.setattr("apps.live.api.exchange_code_for_refresh_token", lambda code: None)
    r = Client().get(URL, {"code": "abc", "state": make_state(instructor.id)})
    assert r["Location"].endswith("?google_connected=false")
    assert not GoogleCalendarGrant.objects.exists()
