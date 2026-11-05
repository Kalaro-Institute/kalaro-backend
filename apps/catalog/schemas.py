from ninja import Schema
from pydantic import Field


class ModuleIn(Schema):
    title: str
    order: int = 0


class LessonIn(Schema):
    title: str
    order: int = 0
    content_type: str = "video"
    text_content: str = ""
    video_provider_id: str = ""
    duration_seconds: int = 0


class ModuleUpdateIn(Schema):
    title: str | None = None
    order: int | None = None


class LessonUpdateIn(Schema):
    title: str | None = None
    order: int | None = None
    content_type: str | None = None
    text_content: str | None = None
    video_provider_id: str | None = None
    duration_seconds: int | None = None


class CourseCreateIn(Schema):
    title: str
    summary: str = ""
    description: str = ""
    objectives: list[str] = []
    price: float = 0
    currency: str = "NGN"
    duration_weeks: int | None = None
    target_roles: list[str] = []
    target_experience: list[str] = []
    target_goals: list[str] = []


class CourseUpdateIn(Schema):
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    objectives: list[str] | None = None
    price: float | None = None
    duration_weeks: int | None = None
    status: str | None = None
    target_roles: list[str] | None = None
    target_experience: list[str] | None = None
    target_goals: list[str] | None = None


class ReorderIn(Schema):
    ordered_ids: list[int]


# ---- outputs ----

class LessonOut(Schema):
    id: int
    title: str
    order: int
    content_type: str
    duration_seconds: int
    # video_provider_id and text_content deliberately excluded —
    # gated behind enrollment, added back in module 4


class ModuleBriefOut(Schema):
    id: int
    course_id: int
    title: str
    order: int


class LessonBriefOut(Schema):
    id: int
    module_id: int
    title: str
    order: int
    content_type: str
    text_content: str
    video_provider_id: str
    duration_seconds: int


class ModuleOut(Schema):
    id: int
    title: str
    order: int
    lessons: list[LessonOut]


class CourseListOut(Schema):
    id: int
    title: str
    slug: str
    summary: str
    price: float
    currency: str
    status: str
    cover_image: str | None = None

    @staticmethod
    def resolve_cover_image(obj):
        return obj.cover_image.url if obj.cover_image else None


class CourseDetailOut(CourseListOut):
    description: str
    objectives: list[str]
    duration_weeks: int | None
    instructor_id: int
    modules: list[ModuleOut]


class ResourceOut(Schema):
    id: int
    title: str
    file_url: str
    size_bytes: int

    @staticmethod
    def resolve_file_url(obj):
        return obj.file.url