from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja import Router, File, Form
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.accounts.auth import admin, any_user, instructor

from .models import Course, Lesson, Module, Resource
from .permissions import check_course_owner, check_enrolled
from .schemas import (
    CourseCreateIn,
    CourseDetailOut,
    CourseListOut,
    CourseUpdateIn,
    LessonBriefOut,
    LessonIn,
    LessonUpdateIn,
    ModuleBriefOut,
    ModuleIn,
    ModuleUpdateIn,
    ReorderIn,
    ResourceOut,
)
from .video import create_stream_upload, get_playback_info
from .progress import mark_lesson_complete

router = Router(tags=["catalog"])


# ---------------- public / student browsing ----------------

@router.get("/courses", response=list[CourseListOut], auth=None)
def list_courses(request):
    return Course.objects.filter(status=Course.Status.PUBLISHED)


@router.get("/courses/{slug}", response=CourseDetailOut, auth=None)
def get_course(request, slug: str):
    course = get_object_or_404(
        Course.objects.prefetch_related("modules__lessons"),
        slug=slug,
        status=Course.Status.PUBLISHED,
    )
    return course


@router.get("/courses/recommended", response=list[CourseListOut], auth=any_user)
def recommended_courses(request):
    profile = getattr(request.auth, "profile", None)
    qs = Course.objects.filter(status=Course.Status.PUBLISHED)
    if profile and profile.current_role:
        qs = qs.filter(target_roles__contains=profile.current_role)
    return qs[:10]


# ---------------- instructor authoring ----------------

@router.post("/courses", response={201: CourseDetailOut}, auth=instructor)
def create_course(request, payload: CourseCreateIn):
    course = Course.objects.create(instructor=request.auth, **payload.dict())
    return 201, course


@router.get("/instructor/courses", response=list[CourseListOut], auth=instructor)
def my_courses(request):
    qs = Course.objects.all()
    if request.auth.role != "admin":
        qs = qs.filter(instructor=request.auth)
    return qs


@router.patch("/courses/{course_id}", response=CourseDetailOut, auth=instructor)
def update_course(request, course_id: int, payload: CourseUpdateIn):
    course = get_object_or_404(Course, pk=course_id)
    check_course_owner(request.auth, course)

    data = payload.dict(exclude_unset=True)
    for field, value in data.items():
        setattr(course, field, value)
    course.save()
    return course


@router.post("/courses/{course_id}/publish", response=CourseDetailOut, auth=instructor)
def publish_course(request, course_id: int):
    course = get_object_or_404(Course, pk=course_id)
    check_course_owner(request.auth, course)

    if not course.modules.exists():
        raise HttpError(400, "A course needs at least one module before it can be published.")
    course.status = Course.Status.PUBLISHED
    course.save(update_fields=["status"])
    return course


@router.post("/courses/{course_id}/cover-image", response=CourseDetailOut, auth=instructor)
def upload_cover_image(request, course_id: int, image: UploadedFile = File(...)):
    course = get_object_or_404(Course, pk=course_id)
    check_course_owner(request.auth, course)

    course.cover_image = image
    course.save(update_fields=["cover_image"])
    return course


# ---------------- modules ----------------

@router.post("/courses/{course_id}/modules", response={201: dict}, auth=instructor)
def add_module(request, course_id: int, payload: ModuleIn):
    course = get_object_or_404(Course, pk=course_id)
    check_course_owner(request.auth, course)
    module = Module.objects.create(course=course, **payload.dict())
    return 201, {"id": module.id, "title": module.title, "order": module.order}


@router.patch("/modules/{module_id}/reorder", response={200: dict}, auth=instructor)
@transaction.atomic
def reorder_modules(request, module_id: int, payload: ReorderIn):
    # module_id here identifies the course via any one module in it,
    # simplest is to look up the course directly instead — left explicit:
    first = get_object_or_404(Module, pk=payload.ordered_ids[0])
    check_course_owner(request.auth, first.course)

    for index, mid in enumerate(payload.ordered_ids):
        Module.objects.filter(pk=mid, course=first.course).update(order=index)
    return {"status": "reordered"}


@router.patch("/modules/{module_id}", response=ModuleBriefOut, auth=instructor)
def update_module(request, module_id: int, payload: ModuleUpdateIn):
    module = get_object_or_404(Module.objects.select_related("course"), pk=module_id)
    check_course_owner(request.auth, module.course)

    data = payload.dict(exclude_unset=True)
    if data.get("title") is None:
        data.pop("title", None)
    if data.get("order") is None:
        data.pop("order", None)
    if "title" in data and not data["title"].strip():
        raise HttpError(400, "Title cannot be blank.")
    if "order" in data and data["order"] < 0:
        raise HttpError(400, "Order cannot be negative.")
    if "order" in data and Module.objects.filter(
        course=module.course, order=data["order"]
    ).exclude(pk=module.pk).exists():
        raise HttpError(409, "Another module in this course already uses that order.")

    for field, value in data.items():
        setattr(module, field, value)
    module.save()
    return module


@router.delete("/modules/{module_id}", response={200: dict}, auth=instructor)
def delete_module(request, module_id: int):
    module = get_object_or_404(Module.objects.select_related("course"), pk=module_id)
    check_course_owner(request.auth, module.course)
    module.delete()  # lessons, resources, quizzes and assignments cascade
    return {"status": "deleted"}


# ---------------- lessons ----------------

@router.post("/modules/{module_id}/lessons", response={201: dict}, auth=instructor)
def add_lesson(request, module_id: int, payload: LessonIn):
    module = get_object_or_404(Module, pk=module_id)
    check_course_owner(request.auth, module.course)
    lesson = Lesson.objects.create(module=module, **payload.dict())
    return 201, {"id": lesson.id, "title": lesson.title}


@router.patch("/lessons/{lesson_id}", response=LessonBriefOut, auth=instructor)
def update_lesson(request, lesson_id: int, payload: LessonUpdateIn):
    lesson = get_object_or_404(Lesson.objects.select_related("module__course"), pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)

    data = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}
    if "title" in data and not data["title"].strip():
        raise HttpError(400, "Title cannot be blank.")
    if "order" in data and data["order"] < 0:
        raise HttpError(400, "Order cannot be negative.")
    if "duration_seconds" in data and data["duration_seconds"] < 0:
        raise HttpError(400, "Duration cannot be negative.")
    if "content_type" in data and data["content_type"] not in Lesson.ContentType.values:
        raise HttpError(
            400, f"content_type must be one of: {', '.join(Lesson.ContentType.values)}."
        )
    if "order" in data and Lesson.objects.filter(
        module=lesson.module, order=data["order"]
    ).exclude(pk=lesson.pk).exists():
        raise HttpError(409, "Another lesson in this module already uses that order.")

    for field, value in data.items():
        setattr(lesson, field, value)
    lesson.save()
    return lesson


@router.delete("/lessons/{lesson_id}", response={200: dict}, auth=instructor)
def delete_lesson(request, lesson_id: int):
    lesson = get_object_or_404(Lesson.objects.select_related("module__course"), pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)
    lesson.delete()  # resources, quiz, assignment and progress entries cascade
    return {"status": "deleted"}


@router.patch("/lessons/{lesson_id}/reorder", response={200: dict}, auth=instructor)
@transaction.atomic
def reorder_lessons(request, lesson_id: int, payload: ReorderIn):
    first = get_object_or_404(Lesson, pk=payload.ordered_ids[0])
    check_course_owner(request.auth, first.module.course)

    for index, lid in enumerate(payload.ordered_ids):
        Lesson.objects.filter(pk=lid, module=first.module).update(order=index)
    return {"status": "reordered"}


@router.get("/lessons/{lesson_id}/playback", auth=any_user)
def get_lesson_playback(request, lesson_id: int):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    course = lesson.module.course

    if request.auth.role in ("instructor", "admin"):
        check_course_owner(request.auth, course)  # ownership, not enrollment
    else:
        check_enrolled(request.auth, course)  # the real gate, replacing the TODO

    playback = get_playback_info(lesson.video_provider_id)
    if not playback["ready"]:
        raise HttpError(409, "This video is still processing.")
    return playback


@router.post("/lessons/{lesson_id}/complete", auth=any_user)
def complete_lesson(request, lesson_id: int):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_enrolled(request.auth, lesson.module.course)

    progress = mark_lesson_complete(user=request.auth, lesson=lesson)
    return {"completed_at": progress.completed_at}


@router.get("/courses/{course_id}/progress", auth=any_user)
def get_course_progress(request, course_id: int):
    from .models import CourseProgress
    course = get_object_or_404(Course, pk=course_id)
    check_enrolled(request.auth, course)

    cp = CourseProgress.objects.filter(user=request.auth, course=course).first()
    return {
        "percent": cp.percent if cp else 0,
        "completed_at": cp.completed_at if cp else None,
    }


@router.post("/lessons/{lesson_id}/resources", response={201: ResourceOut}, auth=instructor)
def add_resource(request, lesson_id: int, title: str = Form(...), file: UploadedFile = File(...)):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)

    resource = Resource.objects.create(
        lesson=lesson, title=title, file=file, size_bytes=file.size,
    )
    return 201, resource


@router.delete("/resources/{resource_id}", auth=instructor)
def delete_resource(request, resource_id: int):
    resource = get_object_or_404(Resource, pk=resource_id)
    check_course_owner(request.auth, resource.lesson.module.course)
    resource.delete()
    return {"status": "deleted"}


@router.delete(
    "/lessons/{lesson_id}/resources/{resource_id}", response={200: dict}, auth=instructor
)
def delete_lesson_resource(request, lesson_id: int, resource_id: int):
    resource = get_object_or_404(
        Resource.objects.select_related("lesson__module__course"),
        pk=resource_id, lesson_id=lesson_id,
    )
    check_course_owner(request.auth, resource.lesson.module.course)
    resource.file.delete(save=False)  # remove the stored file as well as the row
    resource.delete()
    return {"status": "deleted"}


@router.get("/lessons/{lesson_id}/resources", response=list[ResourceOut], auth=any_user)
def list_resources(request, lesson_id: int):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    if request.auth.role in ("instructor", "admin"):
        check_course_owner(request.auth, lesson.module.course)
    else:
        check_enrolled(request.auth, lesson.module.course)
    return lesson.resources.all()

# ---------------- admin ----------------

@router.get("/admin/courses", response=list[CourseListOut], auth=admin)
def admin_list_all_courses(request, status: str | None = None):
    qs = Course.objects.all()
    if status:
        qs = qs.filter(status=status)
    return qs

@router.post("/lessons/{lesson_id}/video-upload-url", auth=instructor)
def get_video_upload_url(request, lesson_id: int):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    check_course_owner(request.auth, lesson.module.course)

    upload = create_stream_upload(title=lesson.title)
    lesson.video_provider_id = upload["video_id"]
    lesson.save(update_fields=["video_provider_id"])

    return {
        "video_id": upload["video_id"],
        "upload_url": upload["upload_url"],
    }
