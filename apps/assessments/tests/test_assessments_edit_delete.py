import pytest
from django.test import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from apps.accounts.models import Role, User
from apps.assessments.models import (
    Answer,
    Assignment,
    Choice,
    Question,
    Quiz,
    QuizAttempt,
)
from apps.catalog.models import Course, Lesson, Module
from apps.payments.models import Enrollment

pytestmark = pytest.mark.django_db

API = "/api/v1/assessments"


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
def student():
    return User.objects.create_user(email="st@test.com", password="supersecret123",
                                    role=Role.STUDENT, email_verified=True)


@pytest.fixture
def lesson(owner):
    course = Course.objects.create(title="C", instructor=owner)
    module = Module.objects.create(course=course, title="M")
    return Lesson.objects.create(module=module, title="L")


@pytest.fixture
def quiz(lesson):
    return Quiz.objects.create(lesson=lesson, title="Q")


@pytest.fixture
def mcq(quiz):
    q = Question.objects.create(quiz=quiz, text="2+2?", type="mcq", points=2)
    Choice.objects.create(question=q, text="3", is_correct=False)
    Choice.objects.create(question=q, text="4", is_correct=True)
    return q


# ---- quizzes ----

def test_update_quiz(owner, quiz):
    r = patch(owner, f"{API}/quizzes/{quiz.id}",
              {"title": "New", "pass_percent": 80, "max_attempts": 3, "time_limit_seconds": 600})
    assert r.status_code == 200
    quiz.refresh_from_db()
    assert (quiz.title, quiz.pass_percent, quiz.max_attempts, quiz.time_limit_seconds) == (
        "New", 80, 3, 600)


def test_update_quiz_can_clear_time_limit(owner, quiz):
    quiz.time_limit_seconds = 300
    quiz.save()
    assert patch(owner, f"{API}/quizzes/{quiz.id}", {"time_limit_seconds": None}).status_code == 200
    quiz.refresh_from_db()
    assert quiz.time_limit_seconds is None


def test_update_quiz_validation(owner, quiz):
    assert patch(owner, f"{API}/quizzes/{quiz.id}", {"pass_percent": 150}).status_code == 400
    assert patch(owner, f"{API}/quizzes/{quiz.id}", {"max_attempts": 0}).status_code == 400


def test_update_quiz_forbidden_for_non_owner(stranger, quiz):
    assert patch(stranger, f"{API}/quizzes/{quiz.id}", {"title": "x"}).status_code == 403


def test_delete_quiz(owner, quiz, mcq):
    assert Client().delete(f"{API}/quizzes/{quiz.id}", **auth(owner)).status_code == 200
    assert not Quiz.objects.filter(pk=quiz.id).exists()
    assert not Question.objects.filter(pk=mcq.id).exists()


def test_delete_quiz_forbidden_for_student(student, quiz):
    assert Client().delete(f"{API}/quizzes/{quiz.id}", **auth(student)).status_code == 403
    assert Quiz.objects.filter(pk=quiz.id).exists()


# ---- questions ----

def test_update_question_text_and_points(owner, mcq):
    r = patch(owner, f"{API}/quiz-questions/{mcq.id}", {"text": "Two plus two?", "points": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Two plus two?" and body["points"] == 5
    assert any(c["is_correct"] for c in body["choices"])


def test_update_question_replaces_choices(owner, mcq):
    r = patch(owner, f"{API}/quiz-questions/{mcq.id}", {"choices": [
        {"text": "A", "is_correct": True, "order": 0},
        {"text": "B", "is_correct": False, "order": 1},
    ]})
    assert r.status_code == 200
    assert sorted(c.text for c in mcq.choices.all()) == ["A", "B"]


def test_update_question_mcq_needs_correct_choice(owner, mcq):
    r = patch(owner, f"{API}/quiz-questions/{mcq.id}",
              {"choices": [{"text": "A", "is_correct": False}]})
    assert r.status_code == 400
    assert mcq.choices.count() == 2  # untouched


def test_update_question_structure_locked_after_answers(owner, mcq, quiz, student):
    attempt = QuizAttempt.objects.create(quiz=quiz, user=student)
    Answer.objects.create(attempt=attempt, question=mcq, selected_choice=mcq.choices.first())
    r = patch(owner, f"{API}/quiz-questions/{mcq.id}",
              {"choices": [{"text": "A", "is_correct": True}]})
    assert r.status_code == 409
    # plain text edits are still fine
    assert patch(owner, f"{API}/quiz-questions/{mcq.id}", {"text": "typo fix"}).status_code == 200


def test_update_question_forbidden_for_non_owner(stranger, mcq):
    assert patch(stranger, f"{API}/quiz-questions/{mcq.id}", {"text": "x"}).status_code == 403


def test_delete_question(owner, mcq):
    assert Client().delete(f"{API}/quiz-questions/{mcq.id}", **auth(owner)).status_code == 200
    assert not Question.objects.filter(pk=mcq.id).exists()


def test_delete_question_blocked_after_answers(owner, mcq, quiz, student):
    attempt = QuizAttempt.objects.create(quiz=quiz, user=student)
    Answer.objects.create(attempt=attempt, question=mcq, selected_choice=mcq.choices.first())
    assert Client().delete(f"{API}/quiz-questions/{mcq.id}", **auth(owner)).status_code == 409
    assert Question.objects.filter(pk=mcq.id).exists()


def test_delete_question_forbidden_for_non_owner(stranger, mcq):
    assert Client().delete(f"{API}/quiz-questions/{mcq.id}", **auth(stranger)).status_code == 403


# ---- assignments ----

@pytest.fixture
def assignment(lesson):
    return Assignment.objects.create(lesson=lesson, title="A", instructions="do it",
                                     due_at=timezone.now())


def test_get_assignment_owner(owner, lesson, assignment):
    r = Client().get(f"{API}/lessons/{lesson.id}/assignment", **auth(owner))
    assert r.status_code == 200
    assert r.json()["id"] == assignment.id and r.json()["title"] == "A"


def test_get_assignment_enrolled_student(student, lesson, assignment):
    Enrollment.objects.create(user=student, course=lesson.module.course,
                              source=Enrollment.Source.ADMIN_GRANT, status=Enrollment.Status.ACTIVE)
    assert Client().get(f"{API}/lessons/{lesson.id}/assignment", **auth(student)).status_code == 200


def test_get_assignment_unenrolled_student_forbidden(student, lesson, assignment):
    assert Client().get(f"{API}/lessons/{lesson.id}/assignment", **auth(student)).status_code == 403


def test_get_assignment_404_when_none(owner, lesson):
    assert Client().get(f"{API}/lessons/{lesson.id}/assignment", **auth(owner)).status_code == 404


def test_update_assignment(owner, assignment):
    r = patch(owner, f"{API}/assignments/{assignment.id}",
              {"title": "B", "max_points": 50, "due_at": None})
    assert r.status_code == 200
    assignment.refresh_from_db()
    assert (assignment.title, assignment.max_points, assignment.due_at) == ("B", 50, None)
    assert assignment.instructions == "do it"  # untouched


def test_update_assignment_forbidden_for_non_owner(stranger, assignment):
    assert patch(stranger, f"{API}/assignments/{assignment.id}", {"title": "x"}).status_code == 403


def test_delete_assignment(owner, assignment):
    assert Client().delete(f"{API}/assignments/{assignment.id}", **auth(owner)).status_code == 200
    assert not Assignment.objects.filter(pk=assignment.id).exists()


def test_delete_assignment_forbidden_for_student(student, assignment):
    assert Client().delete(f"{API}/assignments/{assignment.id}", **auth(student)).status_code == 403
