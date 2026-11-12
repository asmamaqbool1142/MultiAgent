"""Login, signup, Google SSO, and the middleware that keeps the rest of the app private."""

import os
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from backend import auth
from backend.conversations import claim_legacy_conversations
from backend.google_oauth import OAUTH_STATE_TTL_SECONDS, _new_flow, claim_legacy_token, fetch_token

router = APIRouter()

SSO_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]
PUBLIC_PATHS = {"/", "/health", "/favicon.ico"}
PUBLIC_PREFIXES = ("/api/auth/", "/auth/sso/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_sso_pending: dict[str, tuple[object, float]] = {}


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_session(request: Request, response: Response, user_id: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session(user_id),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )


def _finish_signup(user: dict[str, str], first_user: bool) -> None:
    """The first account inherits data saved before accounts existed."""
    if first_user:
        claim_legacy_conversations(user["id"])
        claim_legacy_token(user["id"])


def _client_key(request: Request, email: str) -> str:
    # The last entry is the one our own proxy appended; earlier ones are client-supplied.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    host = forwarded or (request.client.host if request.client else "unknown")
    return f"{host}|{email.strip().lower()}"


def _sso_redirect_uri() -> str:
    explicit = os.getenv("SSO_REDIRECT_URI")
    if explicit:
        return explicit
    base = urlsplit(os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8001/auth/google/callback"))
    return base._replace(path="/auth/sso/google/callback", query="", fragment="").geturl()


def google_sso_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


class SignupRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    name: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


@router.get("/api/auth/me")
def me(request: Request) -> dict[str, object]:
    user = auth.user_for_session(request.cookies.get(auth.SESSION_COOKIE))
    return {"user": user, "google_sso": google_sso_configured()}


@router.post("/api/auth/signup")
async def signup(body: SignupRequest, request: Request) -> JSONResponse:
    try:
        auth.throttle(_client_key(request, body.email))
        first_user = auth.user_count() == 0
        user = await run_in_threadpool(auth.create_user, body.email, body.password, body.name)
    except auth.AuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _finish_signup(user, first_user)
    response = JSONResponse({"user": {"email": user["email"], "name": user["name"]}})
    _set_session(request, response, user["id"])
    return response


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    try:
        auth.throttle(_client_key(request, body.email))
        user = await run_in_threadpool(auth.authenticate, body.email, body.password)
    except auth.AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    response = JSONResponse({"user": {"email": user["email"], "name": user["name"]}})
    _set_session(request, response, user["id"])
    return response


@router.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.get("/auth/sso/google/start", include_in_schema=False)
def sso_start() -> RedirectResponse:
    if not google_sso_configured():
        return RedirectResponse("/?error=Google+sign-in+is+not+configured+on+this+server.")
    now = time.time()
    for state, (_, expires_at) in list(_sso_pending.items()):
        if expires_at <= now:
            del _sso_pending[state]
    flow = _new_flow(SSO_SCOPES)
    flow.redirect_uri = _sso_redirect_uri()
    url, state = flow.authorization_url(prompt="select_account")
    _sso_pending[state] = (flow, now + OAUTH_STATE_TTL_SECONDS)
    return RedirectResponse(url)


@router.get("/auth/sso/google/callback", include_in_schema=False)
def sso_callback(request: Request) -> RedirectResponse:
    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(f"/?error={message.replace(' ', '+')}")

    if request.query_params.get("error"):
        return fail("Google sign-in was cancelled.")
    pending = _sso_pending.pop(request.query_params.get("state", ""), None)
    if pending is None or pending[1] <= time.time():
        return fail("Google sign-in expired. Try again.")
    flow = pending[0]
    try:
        fetch_token(flow, str(request.url))
        info = flow.oauth2session.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15).json()
        if not info.get("email") or not info.get("email_verified") or not info.get("sub"):
            return fail("Google did not confirm a verified email for this account.")
        first_user = auth.user_count() == 0
        user = auth.sign_in_with_google(info["email"], info["sub"], info.get("name", ""))
    except auth.AuthError as error:
        return fail(str(error))
    except ValueError as error:
        return fail(str(error))
    except Exception:
        return fail("Google sign-in failed. Try again.")
    _finish_signup(user, first_user)
    response = RedirectResponse("/app")
    _set_session(request, response, user["id"])
    return response


async def auth_middleware(request: Request, call_next):
    """Require a session for everything except the homepage and auth endpoints."""
    path = request.url.path
    user = auth.user_for_session(request.cookies.get(auth.SESSION_COOKIE))

    # Cookies ride along on cross-site requests; refuse state changes from other origins.
    origin = request.headers.get("origin")
    if origin and request.method not in SAFE_METHODS:
        allowed = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if urlsplit(origin).netloc not in allowed:
            return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)

    public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
    if not public and user is None:
        if request.method == "GET" and path == "/app":
            return RedirectResponse("/")
        return JSONResponse({"detail": "Sign in required."}, status_code=401)

    token = auth.current_user_id.set(user["id"] if user else "")
    try:
        response = await call_next(request)
    finally:
        auth.current_user_id.reset(token)
    if path == "/app":
        response.headers["Cache-Control"] = "no-store"
    return response
