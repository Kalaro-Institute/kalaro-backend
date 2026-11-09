from apps.live.google_calendar import build_auth_flow, get_authorization_url


def test_auth_url_has_no_pkce_challenge(settings):
    """connect and callback use different Flow objects, so PKCE would break the exchange."""
    settings.GOOGLE_OAUTH_CLIENT_ID = "id.apps.googleusercontent.com"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    url = get_authorization_url("state123")
    assert "client_id=id.apps.googleusercontent.com" in url
    assert "code_challenge" not in url
    assert "access_type=offline" in url and "prompt=consent" in url


def test_callback_flow_needs_no_verifier(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    flow = build_auth_flow()
    flow.authorization_url(state="s")
    assert flow.code_verifier is None
