import logging

from django.conf import settings
from django.core import signing
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from apps.accounts.auth import any_user, instructor
from apps.catalog.models import Course
from apps.catalog.permissions import check_course_owner, check_enrolled
from apps.notifications.services import notify

from .google_calendar import (
    create_meet_event,
    exchange_code_for_refresh_token,
    get_authorization_url,
    make_state,
    read_state,
)
from .models import Attendance, GoogleCalendarGrant, LiveSession
from .schemas import SessionCreateIn, SessionOut

logger = logging.getLogger(__name__)

router = Router(tags=["live"])


@router.get("/google/connect", auth=instructor)
def connect_google_calendar(request):
    state = make_state(request.auth.id)
    return {"authorization_url": get_authorization_url(state)}


def _back_to_frontend(connected: bool) -> HttpResponseRedirect:
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    flag = "true" if connected else "false"
    return HttpResponseRedirect(
        f"{base}{settings.GOOGLE_CALENDAR_RETURN_PATH}?google_connected={flag}"
    )


@router.get("/google/callback", auth=None)
def google_callback(
    request, code: str | None = None, state: str | None = None, error: str | None = None
):
    """Google sends the browser here. Always finish by sending the person back to the
    frontend, with ?google_connected=true or ?google_connected=false, so they never see a raw error."""
    if error or not code or not state:  # e.g. the user clicked Cancel on Google's screen
        return _back_to_frontend(False)
    try:
        user_id = read_state(state)  # raises BadSignature (incl. expired) if tampered/old
    except signing.BadSignature:
        return _back_to_frontend(False)

    try:
        refresh_token = exchange_code_for_refresh_token(code)
    except Exception:
        logger.exception("Google Calendar token exchange failed")
        return _back_to_frontend(False)
    if not refresh_token:
        return _back_to_frontend(False)

    GoogleCalendarGrant.objects.update_or_create(
        user_id=user_id, defaults={"refresh_token": refresh_token},
    )
    return _back_to_frontend(True)


@router.post("/courses/{course_id}/sessions", response={201: SessionOut}, auth=instructor)
def schedule_session(request, course_id: int, payload: SessionCreateIn):
    course = get_object_or_404(Course, pk=course_id)
    check_course_owner(request.auth, course)

    grant = GoogleCalendarGrant.objects.filter(user=request.auth).first()
    if not grant:
        raise HttpError(400, "Connect your Google Calendar before scheduling a class.")

    meet = create_meet_event(
        refresh_token=grant.refresh_token,
        title=payload.title,
        start=payload.scheduled_at,
        duration_minutes=payload.duration_minutes,
    )

    session = LiveSession.objects.create(
        course=course, instructor=request.auth, title=payload.title,
        scheduled_at=payload.scheduled_at, duration_minutes=payload.duration_minutes,
        meet_link=meet["meet_link"], calendar_event_id=meet["event_id"],
    )

    from apps.payments.models import Enrollment
    for enrollment in Enrollment.objects.filter(course=course, status="active").select_related("user"):
        notify(
            user=enrollment.user, type="class_reminder",
            title=f"New live class scheduled: {payload.title}",
            body=f"{payload.title} is scheduled for {payload.scheduled_at:%b %d, %Y at %H:%M}.",
            link=f"/courses/{course.slug}/live/{session.id}",
        )

    return 201, session


@router.get("/courses/{course_id}/sessions", response=list[SessionOut], auth=any_user)
def list_sessions(request, course_id: int):
    course = get_object_or_404(Course, pk=course_id)
    if request.auth.role in ("instructor", "admin"):
        check_course_owner(request.auth, course)
    else:
        check_enrolled(request.auth, course)
    return LiveSession.objects.filter(course=course)


@router.post("/sessions/{session_id}/join", auth=any_user)
def join_session(request, session_id: int):
    session = get_object_or_404(LiveSession, pk=session_id)
    if request.auth.role not in ("instructor", "admin"):
        check_enrolled(request.auth, session.course)

    Attendance.objects.get_or_create(session=session, user=request.auth)
    return {"meet_link": session.meet_link}