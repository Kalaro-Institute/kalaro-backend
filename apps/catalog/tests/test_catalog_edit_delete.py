import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from apps.accounts.models import Role, User
from apps.catalog.models import Course, Lesson, Module, Resource

pytestmark = pytest.mark.django_db

API = "/api/v1/catalog"


def auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def patch(user, url, body):
    return Client().patch(url, data=body, content_type="application/json", **auth(user))


@pytest.fixture
def owner():
    return User.objects.create_user(email="o@test.com", password="supersecret123",
                                    role=Role.INSTRUCTOR, email_verified=True)


@pytest.fixture
def stranger():
    return User.objects.create_user(email="s@test.com", password="supersecret123",
                                    role=Role.INSTRUCTOR, email_verified=True)


@pytest.fixture
def tree(owner):
    course = Course.objects.create(title="C", instructor=owner)
    m1 = Module.objects.create(course=course, title="M1", order=0)
    m2 = Module.objects.create(course=course, title="M2", order=1)
    l1 = Lesson.objects.create(module=m1, title="L1", order=0)
    l2 = Lesson.objects.create(module=m1, title="L2", order=1)
    return course, m1, m2, l1, l2


def test_update_module(owner, tree):
    _, m1, *_ = tree
    r = patch(owner, f"{API}/modules/{m1.id}", {"title": "Renamed"})
    assert r.status_code == 200
    m1.refresh_from_db()
    assert m1.title == "Renamed" and m1.order == 0


def test_update_module_order_conflict(owner, tree):
    _, m1, *_ = tree
    assert patch(owner, f"{API}/modules/{m1.id}", {"order": 1}).status_code == 409


def test_update_module_forbidden_for_non_owner(stranger, tree):
    _, m1, *_ = tree
    assert patch(stranger, f"{API}/modules/{m1.id}", {"title": "x"}).status_code == 403


def test_delete_module_cascades(owner, tree):
    _, m1, _, l1, _ = tree
    r = Client().delete(f"{API}/modules/{m1.id}", **auth(owner))
    assert r.status_code == 200
    assert not Module.objects.filter(pk=m1.id).exists()
    assert not Lesson.objects.filter(pk=l1.id).exists()


def test_delete_module_forbidden_for_non_owner(stranger, tree):
    _, m1, *_ = tree
    assert Client().delete(f"{API}/modules/{m1.id}", **auth(stranger)).status_code == 403
    assert Module.objects.filter(pk=m1.id).exists()


def test_update_lesson(owner, tree):
    *_, l1, _ = tree
    r = patch(owner, f"{API}/lessons/{l1.id}", {"title": "New", "content_type": "text",
                                                "text_content": "hello"})
    assert r.status_code == 200
    l1.refresh_from_db()
    assert (l1.title, l1.content_type, l1.text_content) == ("New", "text", "hello")


def test_update_lesson_invalid_content_type(owner, tree):
    *_, l1, _ = tree
    assert patch(owner, f"{API}/lessons/{l1.id}", {"content_type": "podcast"}).status_code == 400


def test_update_lesson_order_conflict(owner, tree):
    *_, l1, _ = tree
    assert patch(owner, f"{API}/lessons/{l1.id}", {"order": 1}).status_code == 409


def test_delete_lesson(owner, tree):
    *_, l1, _ = tree
    assert Client().delete(f"{API}/lessons/{l1.id}", **auth(owner)).status_code == 200
    assert not Lesson.objects.filter(pk=l1.id).exists()


def test_delete_lesson_forbidden_for_non_owner(stranger, tree):
    *_, l1, _ = tree
    assert Client().delete(f"{API}/lessons/{l1.id}", **auth(stranger)).status_code == 403


def test_delete_lesson_resource(owner, tree, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    *_, l1, l2 = tree
    res = Resource.objects.create(lesson=l1, title="r",
                                  file=SimpleUploadedFile("a.txt", b"hi"), size_bytes=2)
    # resource belongs to l1, so addressing it through l2 must 404
    assert Client().delete(f"{API}/lessons/{l2.id}/resources/{res.id}",
                           **auth(owner)).status_code == 404
    assert Client().delete(f"{API}/lessons/{l1.id}/resources/{res.id}",
                           **auth(owner)).status_code == 200
    assert not Resource.objects.filter(pk=res.id).exists()


def test_delete_lesson_resource_forbidden_for_non_owner(stranger, tree, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    *_, l1, _ = tree
    res = Resource.objects.create(lesson=l1, title="r",
                                  file=SimpleUploadedFile("a.txt", b"hi"), size_bytes=2)
    assert Client().delete(f"{API}/lessons/{l1.id}/resources/{res.id}",
                           **auth(stranger)).status_code == 403
    assert Resource.objects.filter(pk=res.id).exists()
