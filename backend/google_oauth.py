"""Small, local OAuth flow for read-only Google Calendar and Gmail access."""

import json
import os
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from google_auth_oauthlib.flow import Flow

from backend.auth import current_user_id, load_google_token, save_google_token

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]
CALENDAR_WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_WRITE_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
ACCOUNT_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
SERVICE_SCOPES = {
    "calendar_read": CALENDAR_SCOPES,
    "calendar_write": CALENDAR_WRITE_SCOPES,
    "email": GMAIL_SCOPES,
    "email_send": GMAIL_WRITE_SCOPES,
}
GOOGLE_SCOPES = ACCOUNT_SCOPES + CALENDAR_SCOPES + CALENDAR_WRITE_SCOPES + GMAIL_SCOPES
# Pre-accounts installs kept one token in this file; claim_legacy_token() moves it
# into the database for the first account.
LEGACY_TOKEN_PATH = Path(os.getenv("GOOGLE_TOKEN_PATH", "data/google_calendar_token.json"))
OAUTH_STATE_TTL_SECONDS = 600

# The app is a single local Docker service, so short-lived flow state can live
# in memory. Expiration and one-time removal prevent stale callbacks being reused.
_pending_flows: dict[str, tuple[Flow, float, str, str]] = {}
logger = logging.getLogger(__name__)


def _uid() -> str:
    user_id = current_user_id.get()
    if not user_id:
        raise PermissionError("Sign in required.")
    return user_id


def load_token_info() -> dict | None:
    """The signed-in user's saved Google credentials, or None if never connected."""
    raw = load_google_token(_uid())
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def save_token(token_json: str) -> None:
    save_google_token(_uid(), token_json)


def claim_legacy_token(user_id: str) -> None:
    """Move the pre-accounts token file to the first user so they stay connected."""
    if LEGACY_TOKEN_PATH.is_file():
        save_google_token(user_id, LEGACY_TOKEN_PATH.read_text(encoding="utf-8"))
        LEGACY_TOKEN_PATH.unlink()


def _oauth_error_code(error: Exception) -> str | None:
    """Extract only Google's short error code, never its response or request data."""
    code = getattr(error, "error", None)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            code = response.json().get("error") or code
        except (AttributeError, ValueError):
            pass
    return code if isinstance(code, str) else None


def _new_flow(scopes: list[str], state: str | None = None) -> Flow:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=scopes,
        state=state,
    )
    flow.redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:8001/auth/google/callback",
    )
    return flow


def start_google_oauth(service: str = "all") -> str:
    """Return a consent URL and retain its state until the callback arrives."""
    now = time.time()
    for old_state, (_, expires_at, _, _) in list(_pending_flows.items()):
        if expires_at <= now:
            del _pending_flows[old_state]

    if service != "all" and service not in SERVICE_SCOPES:
        raise ValueError("Unknown Google permission service.")
    requested = GOOGLE_SCOPES if service == "all" else ACCOUNT_SCOPES + SERVICE_SCOPES[service]
    # Preserve scopes already granted when Google performs incremental consent.
    info = load_token_info()
    if info:
        try:
            from google.oauth2.credentials import Credentials
            existing = Credentials.from_authorized_user_info(info).scopes or []
            requested = list(dict.fromkeys([*requested, *existing]))
        except ValueError:
            pass
    flow = _new_flow(requested)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent" if service == "all" else "consent",
    )
    _pending_flows[state] = (flow, now + OAUTH_STATE_TTL_SECONDS, service, current_user_id.get())
    return authorization_url


def fetch_token(flow: Flow, authorization_response: str) -> None:
    """Exchange Google's one-time code, raising ValueError with a user-safe message."""
    redirect = urlsplit(flow.redirect_uri)
    allow_local_http = (
        redirect.scheme == "http"
        and redirect.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    previous_insecure_transport = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    try:
        if allow_local_http:
            # OAuthlib blocks HTTP by default; Google requires HTTP for loopback apps.
            # Scope its development exception to this token exchange only.
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(authorization_response=authorization_response)
    except Exception as error:
        # Only log Google's short error code. Response text can contain sensitive data.
        code = _oauth_error_code(error)
        logger.warning("Google OAuth token exchange failed (%s).", code or type(error).__name__)
        messages = {
            "invalid_client": "Google rejected the OAuth client credentials. Check the Web client ID and secret in .env.",
            "redirect_uri_mismatch": "Google rejected the callback URL. Check that the exact redirect URI is registered for this OAuth client.",
            "invalid_grant": "Google rejected this one-time sign-in code. Start a fresh sign-in and try again.",
        }
        detail = messages.get(code, "Google could not complete sign-in. Check the OAuth client settings and try again.")
        raise ValueError(detail) from error
    finally:
        if allow_local_http:
            if previous_insecure_transport is None:
                os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous_insecure_transport


def complete_google_oauth(authorization_response: str, state: str) -> str:
    """Validate a one-time state value, exchange the code, and store the token."""
    pending = _pending_flows.pop(state, None)
    if (
        not state
        or pending is None
        or pending[1] <= time.time()
        or pending[3] != current_user_id.get()
    ):
        raise ValueError("Google sign-in expired or state did not match; start again.")

    fetch_token(pending[0], authorization_response)
    save_token(pending[0].credentials.to_json())
    return pending[2]
