import datetime as dt

from django.conf import settings
from django.core import signing
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def build_auth_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_CALENDAR_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_CALENDAR_REDIRECT_URI,
        # The connect and callback requests build separate Flow objects, so an
        # auto-generated PKCE verifier from the first would be lost before the code
        # exchange and Google would reject it ("Missing code verifier"). This is a
        # confidential client (it sends the client secret), so PKCE isn't needed.
        autogenerate_code_verifier=False,
    )


def get_authorization_url(state: str) -> str:
    flow = build_auth_flow()
    url, _ = flow.authorization_url(
        access_type="offline",   # required to get a refresh_token
        prompt="consent",        # forces refresh_token even on repeat consent
        state=state,
    )
    return url


def exchange_code_for_refresh_token(code: str) -> str:
    flow = build_auth_flow()
    flow.fetch_token(code=code)
    return flow.credentials.refresh_token


def create_meet_event(*, refresh_token: str, title: str, start: dt.datetime,
                      duration_minutes: int) -> dict:
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
    )
    service = build("calendar", "v3", credentials=creds)

    end = start + dt.timedelta(minutes=duration_minutes)
    event = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "conferenceData": {
            "createRequest": {
                "requestId": f"kalaro-{start.timestamp()}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    created = service.events().insert(
        calendarId="primary", body=event, conferenceDataVersion=1,
    ).execute()

    return {
        "event_id": created["id"],
        "meet_link": created.get("hangoutLink", ""),
    }

def make_state(user_id: int) -> str:
    return signing.dumps({"user_id": user_id})

def read_state(state: str) -> int:
    return signing.loads(state, max_age=600)["user_id"]
