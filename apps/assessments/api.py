from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from apps.accounts.auth import any_user, instructor
from apps.catalog.models import Lesson
from apps.catalog.permissions import check_course_owner

from .grading import grade_short_answers, submit_quiz_attempt
from .models import Answer, Assignment, Choice, Grade, Question, Quiz, QuizAttempt, Submission
from .permissions import check_course_owner_or_enrolled
from .schemas import (
    AssignmentCreateIn,
    AssignmentOut,
    AssignmentUpdateIn,
    AttemptOut,
    GradeIn,
    GradeShortAnswersIn,
    QuestionAdminOut,
    QuestionIn,
    QuestionUpdateIn,
    QuizCreateIn,
    QuizOut,
    QuizUpdateIn,
    SubmissionOut,
    SubmitAttemptIn,
)
from apps.notifications.services import notify

router = Router(tags=["assessments"])


# ---------------- quiz authoring ----------------

@router.post("/lessons/{lesson_id}/quiz", response={201: QuizOut}, auth=instructor)
def create_quiz(request, lesson_id: int, payload: QuizCreateIn):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)

    if hasattr(lesson, "quiz"):
        raise HttpError(409, "This lesson already has a quiz.")

    quiz = Quiz.objects.create(lesson=lesson, **payload.dict())
    return 201, quiz


@router.post("/quizzes/{quiz_id}/questions", response={201: dict}, auth=instructor)
def add_question(request, quiz_id: int, payload: QuestionIn):
    quiz = get_object_or_404(Quiz, pk=quiz_id)
    check_course_owner(request.auth, quiz.lesson.module.course)

    if payload.type == "mcq" and not any(c.is_correct for c in payload.choices):
        raise HttpError(400, "An MCQ question needs at least one correct choice.")

    question = Question.objects.create(
        quiz=quiz, text=payload.text, type=payload.type,
        order=payload.order, points=payload.points,
    )
    for c in payload.choices:
        Choice.objects.create(question=question, **c.dict())

    return 201, {"id": question.id}


@router.patch("/quizzes/{quiz_id}", response=QuizOut, auth=instructor)
def update_quiz(request, quiz_id: int, payload: QuizUpdateIn):
    quiz = get_object_or_404(Quiz.objects.select_related("lesson__module__course"), pk=quiz_id)
    check_course_owner(request.auth, quiz.lesson.module.course)

    data = payload.dict(exclude_unset=True)
    # Only time_limit_seconds may be explicitly nulled (it means "no limit").
    for field in ("title", "pass_percent", "max_attempts"):
        if field in data and data[field] is None:
            data.pop(field)

    if "title" in data and not data["title"].strip():
        raise HttpError(400, "Title cannot be blank.")
    if "pass_percent" in data and not 0 <= data["pass_percent"] <= 100:
        raise HttpError(400, "pass_percent must be between 0 and 100.")
    if "max_attempts" in data and data["max_attempts"] < 1:
        raise HttpError(400, "max_attempts must be at least 1.")
    if data.get("time_limit_seconds") is not None and data["time_limit_seconds"] < 1:
        raise HttpError(400, "time_limit_seconds must be positive.")

    for field, value in data.items():
        setattr(quiz, field, value)
    quiz.save()
    return quiz


@router.delete("/quizzes/{quiz_id}", response={200: dict}, auth=instructor)
def delete_quiz(request, quiz_id: int):
    quiz = get_object_or_404(Quiz.objects.select_related("lesson__module__course"), pk=quiz_id)
    check_course_owner(request.auth, quiz.lesson.module.course)
    quiz.delete()  # questions, choices, attempts and answers cascade
    return {"status": "deleted"}


@router.patch("/quiz-questions/{question_id}", response=QuestionAdminOut, auth=instructor)
@transaction.atomic
def update_question(request, question_id: int, payload: QuestionUpdateIn):
    question = get_object_or_404(
        Question.objects.select_related("quiz__lesson__module__course"), pk=question_id
    )
    check_course_owner(request.auth, question.quiz.lesson.module.course)

    data = payload.dict(exclude_unset=True)
    choices = data.pop("choices", None)
    data = {k: v for k, v in data.items() if v is not None}

    if "text" in data and not data["text"].strip():
        raise HttpError(400, "Question text cannot be blank.")
    if "order" in data and data["order"] < 0:
        raise HttpError(400, "Order cannot be negative.")
    if "points" in data and data["points"] < 1:
        raise HttpError(400, "points must be at least 1.")
    if "type" in data and data["type"] not in Question.Type.values:
        raise HttpError(400, f"type must be one of: {', '.join(Question.Type.values)}.")

    new_type = data.get("type", question.type)
    changes_structure = choices is not None or new_type != question.type
    if changes_structure and Answer.objects.filter(question=question).exists():
        raise HttpError(
            409,
            "This question already has student answers, so its type and choices can no "
            "longer be changed.",
        )

    if new_type == Question.Type.MCQ:
        if choices is not None:
            has_correct = any(c["is_correct"] for c in choices)
        else:
            has_correct = question.choices.filter(is_correct=True).exists()
        if not has_correct:
            raise HttpError(400, "An MCQ question needs at least one correct choice.")

    for field, value in data.items():
        setattr(question, field, value)
    question.save()

    if choices is not None:
        question.choices.all().delete()
        Choice.objects.bulk_create(
            Choice(question=question, text=c["text"], is_correct=c["is_correct"], order=c["order"])
            for c in choices
        )

    return Question.objects.prefetch_related("choices").get(pk=question.pk)


@router.delete("/quiz-questions/{question_id}", response={200: dict}, auth=instructor)
def delete_question(request, question_id: int):
    question = get_object_or_404(
        Question.objects.select_related("quiz__lesson__module__course"), pk=question_id
    )
    check_course_owner(request.auth, question.quiz.lesson.module.course)

    if Answer.objects.filter(question=question).exists():
        raise HttpError(
            409,
            "This question already has student answers and can no longer be deleted.",
        )
    question.delete()
    return {"status": "deleted"}


# ---------------- quiz taking (student) ----------------

@router.get("/lessons/{lesson_id}/quiz", response=QuizOut, auth=any_user)
def get_quiz(request, lesson_id: int):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_course_owner_or_enrolled(request.auth, lesson.module.course)
    return get_object_or_404(Quiz, lesson=lesson)


@router.post("/quizzes/{quiz_id}/attempts", response={201: AttemptOut}, auth=any_user)
def start_attempt(request, quiz_id: int):
    quiz = get_object_or_404(Quiz, pk=quiz_id)
    check_course_owner_or_enrolled(request.auth, quiz.lesson.module.course)

    if request.auth.role in ("instructor", "admin"):
        raise HttpError(403, "Instructors cannot attempt quizzes.")

    existing_count = QuizAttempt.objects.filter(quiz=quiz, user=request.auth).count()
    if existing_count >= quiz.max_attempts:
        raise HttpError(409, "You have used all your attempts for this quiz.")

    attempt = QuizAttempt.objects.create(quiz=quiz, user=request.auth)
    return 201, attempt


@router.post("/attempts/{attempt_id}/submit", response=AttemptOut, auth=any_user)
def submit_attempt(request, attempt_id: int, payload: SubmitAttemptIn):
    attempt = get_object_or_404(QuizAttempt, pk=attempt_id, user=request.auth)

    for a in payload.answers:
        Answer.objects.update_or_create(
            attempt=attempt, question_id=a.question_id,
            defaults={
                "selected_choice_id": a.selected_choice_id,
                "text_answer": a.text_answer,
            },
        )

    return submit_quiz_attempt(attempt)


@router.get("/my-attempts/{quiz_id}", response=list[AttemptOut], auth=any_user)
def my_attempts(request, quiz_id: int):
    return QuizAttempt.objects.filter(quiz_id=quiz_id, user=request.auth)


# ---------------- instructor grading (short answer) ----------------

@router.get("/quizzes/{quiz_id}/pending-grading", auth=instructor)
def pending_grading(request, quiz_id: int):
    quiz = get_object_or_404(Quiz, pk=quiz_id)
    check_course_owner(request.auth, quiz.lesson.module.course)
    attempts = QuizAttempt.objects.filter(quiz=quiz, status=QuizAttempt.Status.AWAITING_GRADING)
    return [{"attempt_id": a.id, "user_email": a.user.email} for a in attempts]


@router.post("/attempts/{attempt_id}/grade", response=AttemptOut, auth=instructor)
def grade_attempt(request, attempt_id: int, payload: GradeShortAnswersIn):
    attempt = get_object_or_404(QuizAttempt, pk=attempt_id)
    check_course_owner(request.auth, attempt.quiz.lesson.module.course)
    return grade_short_answers(attempt, payload.scores)


# ---------------- assignments ----------------

@router.post("/lessons/{lesson_id}/assignment", response={201: dict}, auth=instructor)
def create_assignment(request, lesson_id: int, payload: AssignmentCreateIn):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)
    if hasattr(lesson, "assignment"):
        raise HttpError(409, "This lesson already has an assignment.")
    assignment = Assignment.objects.create(lesson=lesson, **payload.dict())
    return 201, {"id": assignment.id}


@router.get("/lessons/{lesson_id}/assignment", response=AssignmentOut, auth=any_user)
def get_assignment(request, lesson_id: int):
    lesson = get_object_or_404(Lesson.objects.select_related("module__course"), pk=lesson_id)
    check_course_owner_or_enrolled(request.auth, lesson.module.course)
    return get_object_or_404(Assignment, lesson=lesson)


@router.patch("/assignments/{assignment_id}", response=AssignmentOut, auth=instructor)
def update_assignment(request, assignment_id: int, payload: AssignmentUpdateIn):
    assignment = get_object_or_404(
        Assignment.objects.select_related("lesson__module__course"), pk=assignment_id
    )
    check_course_owner(request.auth, assignment.lesson.module.course)

    data = payload.dict(exclude_unset=True)
    # Only due_at may be explicitly nulled (it means "no deadline").
    for field in ("title", "instructions", "max_points"):
        if field in data and data[field] is None:
            data.pop(field)

    if "title" in data and not data["title"].strip():
        raise HttpError(400, "Title cannot be blank.")
    if "max_points" in data and data["max_points"] < 1:
        raise HttpError(400, "max_points must be at least 1.")

    for field, value in data.items():
        setattr(assignment, field, value)
    assignment.save()
    return assignment


@router.delete("/assignments/{assignment_id}", response={200: dict}, auth=instructor)
def delete_assignment(request, assignment_id: int):
    assignment = get_object_or_404(
        Assignment.objects.select_related("lesson__module__course"), pk=assignment_id
    )
    check_course_owner(request.auth, assignment.lesson.module.course)
    assignment.delete()  # submissions and grades cascade
    return {"status": "deleted"}


@router.post("/assignments/{assignment_id}/submit", response={201: SubmissionOut}, auth=any_user)
def submit_assignment(request, assignment_id: int, text_response: str = "", file=None):
    assignment = get_object_or_404(Assignment, pk=assignment_id)
    check_course_owner_or_enrolled(request.auth, assignment.lesson.module.course)

    if request.auth.role in ("instructor", "admin"):
        raise HttpError(403, "Instructors cannot submit assignments.")

    is_late = bool(assignment.due_at and timezone.now() > assignment.due_at)

    submission, _ = Submission.objects.update_or_create(
        assignment=assignment, user=request.auth,
        defaults={"text_response": text_response, "file": file, "is_late": is_late},
    )
    return 201, submission


@router.post("/submissions/{submission_id}/grade", auth=instructor)
def grade_submission(request, submission_id: int, payload: GradeIn):
    submission = get_object_or_404(Submission, pk=submission_id)
    check_course_owner(request.auth, submission.assignment.lesson.module.course)

    Grade.objects.update_or_create(
        submission=submission,
        defaults={"points": payload.points, "feedback": payload.feedback,
                  "graded_by": request.auth},
    )
    submission.status = Submission.Status.GRADED
    submission.save(update_fields=["status"])

    notify(
        user=submission.user, type="assignment_graded",
        title="Your assignment has been graded",
        body=f"You scored {payload.points} on {submission.assignment.title}.",
        link=f"/assignments/{submission.assignment_id}/results",
    )
    return {"status": "graded"}