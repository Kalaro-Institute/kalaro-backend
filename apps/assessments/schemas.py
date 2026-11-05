from datetime import datetime

from ninja import Schema


class ChoiceIn(Schema):
    text: str
    is_correct: bool = False
    order: int = 0


class QuestionIn(Schema):
    text: str
    type: str = "mcq"
    order: int = 0
    points: int = 1
    choices: list[ChoiceIn] = []


class QuizCreateIn(Schema):
    title: str
    time_limit_seconds: int | None = None
    pass_percent: int = 60
    max_attempts: int = 1


class QuizUpdateIn(Schema):
    title: str | None = None
    time_limit_seconds: int | None = None  # send null explicitly to remove the limit
    pass_percent: int | None = None
    max_attempts: int | None = None


class QuestionUpdateIn(Schema):
    text: str | None = None
    type: str | None = None
    order: int | None = None
    points: int | None = None
    choices: list[ChoiceIn] | None = None  # when sent, replaces ALL existing choices


class AssignmentUpdateIn(Schema):
    title: str | None = None
    instructions: str | None = None
    due_at: datetime | None = None  # send null explicitly to remove the deadline
    max_points: int | None = None


class AnswerIn(Schema):
    question_id: int
    selected_choice_id: int | None = None
    text_answer: str = ""


class SubmitAttemptIn(Schema):
    answers: list[AnswerIn]


class GradeShortAnswersIn(Schema):
    scores: dict[int, float]  # {answer_id: points}


class AssignmentCreateIn(Schema):
    title: str
    instructions: str = ""
    due_at: str | None = None
    max_points: int = 100


class GradeIn(Schema):
    points: float
    feedback: str = ""


# ---- outputs ----

class ChoiceOut(Schema):
    id: int
    text: str
    order: int
    # is_correct deliberately excluded from student-facing output


class QuestionOut(Schema):
    id: int
    text: str
    type: str
    order: int
    points: int
    choices: list[ChoiceOut]


class QuizOut(Schema):
    id: int
    title: str
    time_limit_seconds: int | None
    pass_percent: int
    max_attempts: int
    questions: list[QuestionOut]


class ChoiceAdminOut(Schema):
    id: int
    text: str
    order: int
    is_correct: bool  # instructor-facing only


class QuestionAdminOut(Schema):
    id: int
    quiz_id: int
    text: str
    type: str
    order: int
    points: int
    choices: list[ChoiceAdminOut]


class AssignmentOut(Schema):
    id: int
    lesson_id: int
    title: str
    instructions: str
    due_at: datetime | None
    max_points: int


class AttemptOut(Schema):
    id: int
    status: str
    started_at: str
    submitted_at: str | None
    score_percent: float | None
    passed: bool | None


class SubmissionOut(Schema):
    id: int
    status: str
    submitted_at: str
    is_late: bool
    file_url: str | None = None
    text_response: str

    @staticmethod
    def resolve_file_url(obj):
        return obj.file.url if obj.file else None