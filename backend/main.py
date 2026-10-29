"""Reusable HTTP API for the personal assistant clients."""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Literal

from openai import APIError, OpenAI
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from backend.agent import run_agent, synthesize_agent_answers
from backend.calendar_agent import run_calendar_agent
from backend.calendar_writer import run_calendar_writer
from backend.calendar_tools import (
    CalendarNotConnectedError,
    create_calendar_event,
    get_calendar_service,
    get_google_service,
    google_token_has_scopes,
)
from backend.conversations import (
    create_conversation,
    delete_conversation,
    get_conversation,
    get_model_history,
    initialize_conversations,
    list_conversations,
    get_integration_permission,
    save_turn,
    set_integration_permission,
)
from backend.email_agent import run_email_agent
from backend.google_oauth import (
    ACCOUNT_SCOPES,
    CALENDAR_SCOPES,
    CALENDAR_WRITE_SCOPES,
    GMAIL_SCOPES,
    GMAIL_WRITE_SCOPES,
    complete_google_oauth,
    start_google_oauth,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="Personal Assistant API", version="0.1.0")
FRONTEND_PATH = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.on_event("startup")
def startup() -> None:
    initialize_conversations()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=36)


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str | None = None


class ConversationCreate(BaseModel):
    title: str = Field(default="New chat", max_length=80)


class CalendarEventRequest(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    start: str = Field(min_length=1, max_length=40)
    end: str = Field(min_length=1, max_length=40)
    time_zone: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=4000)


def is_calendar_write_request(message: str) -> bool:
    """Route explicit create and reschedule requests to the write-enabled agent."""
    asks_to_write = re.search(
        r"\b(create|add|schedule|book|put|change|move|reschedule|update|edit)\b",
        message,
        re.IGNORECASE,
    )
    mentions_event = re.search(
        r"\b(calendar|event|meeting|appointment|interview|reminder)\b",
        message,
        re.IGNORECASE,
    )
    return bool(asks_to_write and mentions_event)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the browser dashboard."""
    return FileResponse(FRONTEND_PATH)


@lru_cache(maxsize=1)
def get_model_client() -> OpenAI:
    """Create the Groq client only when chat is used, so health works without a key."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Assistant is not configured. Set GROQ_API_KEY on the server.",
        )
    return OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")


def _log_model_error(error: APIError) -> None:
    """Log the provider's validation reason without logging request headers or prompts."""
    body = getattr(error, "body", None)
    provider_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(provider_error, dict):
        error_type = provider_error.get("type", "unknown")
        message = provider_error.get("message", "")
    else:
        error_type = type(error).__name__
        message = ""

    # Provider validation messages are useful for fixing configuration; keep them
    # short and never log the SDK request, headers, or API key.
    safe_message = message[:300] if isinstance(message, str) else ""
    logger.error(
        "Groq model request failed (HTTP %s, %s): %s",
        getattr(error, "status_code", "unknown"),
        error_type,
        safe_message,
    )


def _gmail_write_service_factory():
    """Expose Gmail writes only when OAuth scope and the user's toggle are enabled."""
    if not google_token_has_scopes(GMAIL_WRITE_SCOPES):
        return None
    if not get_integration_permission("email_send", default=False):
        return None
    return lambda: get_google_service(GMAIL_WRITE_SCOPES, "Gmail write")


def _gmail_services_for_agent():
    """Load each email capability independently so read and send toggles stay separate."""
    write_factory = _gmail_write_service_factory()
    try:
        read_service = get_google_service(GMAIL_SCOPES, "Gmail")
    except CalendarNotConnectedError:
        if write_factory is None:
            raise
        read_service = None
    return read_service, write_factory


def _fallback_answer(
    successful_reports: list[dict[str, str]], failed_reports: int
) -> str:
    """Return useful specialist replies if the final model synthesis fails."""
    answer = "\n\n".join(
        report["answer"].strip()
        for report in successful_reports
        if report.get("answer", "").strip()
    )
    if failed_reports:
        incomplete_note = "One of the connected sources could not be checked this time."
        answer = f"{answer}\n\n- {incomplete_note}" if answer else incomplete_note
    return answer or "I couldn't produce an answer this time. Please try again."


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is running; this does not check Groq access."""
    return {"status": "ok"}


@app.get("/api/conversations")
def conversations_list() -> list[dict[str, str]]:
    """List saved local chat threads, newest first."""
    return list_conversations()


@app.post("/api/conversations")
def conversations_create(request: ConversationCreate) -> dict[str, str]:
    """Create an empty chat thread."""
    return create_conversation(request.title)


@app.get("/api/conversations/{conversation_id}")
def conversation_detail(conversation_id: str) -> dict[str, object]:
    """Load a saved thread and its complete visible history."""
    conversation = get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return conversation


@app.delete("/api/conversations/{conversation_id}")
def conversation_remove(conversation_id: str) -> dict[str, bool]:
    """Delete one local chat thread and its messages."""
    if not delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return {"deleted": True}


@app.get("/api/status")
def api_status() -> dict[str, bool]:
    """Expose setup booleans for the dashboard without returning secret values."""
    calendar_read_authorized = google_token_has_scopes(CALENDAR_SCOPES)
    calendar_write_authorized = google_token_has_scopes(CALENDAR_WRITE_SCOPES)
    email_authorized = google_token_has_scopes(GMAIL_SCOPES)
    email_send_authorized = google_token_has_scopes(GMAIL_WRITE_SCOPES)
    calendar_read_enabled = get_integration_permission("calendar_read", calendar_read_authorized)
    calendar_write_enabled = get_integration_permission("calendar_write", calendar_write_authorized)
    email_enabled = get_integration_permission("email", email_authorized)
    email_send_enabled = get_integration_permission("email_send", default=False)
    return {
        "model_configured": bool(os.getenv("GROQ_API_KEY")),
        "calendar_read_authorized": calendar_read_authorized,
        "calendar_write_authorized": calendar_write_authorized,
        "email_authorized": email_authorized,
        "email_send_authorized": email_send_authorized,
        "calendar_read_enabled": calendar_read_enabled,
        "calendar_write_enabled": calendar_write_enabled,
        "email_enabled": email_enabled,
        "email_send_enabled": email_send_enabled,
        "calendar_connected": calendar_read_authorized and calendar_read_enabled,
        "calendar_write_connected": calendar_write_authorized and calendar_write_enabled,
        "email_connected": email_authorized and email_enabled,
        "email_send_connected": email_send_authorized and email_send_enabled,
    }


@app.get("/api/google/account")
def google_account() -> dict[str, str | None]:
    """Return the email address of the Google account connected to this app."""
    try:
        service = get_google_service(ACCOUNT_SCOPES, "Google account")
        response = service.get(
            "https://openidconnect.googleapis.com/v1/userinfo", timeout=15
        )
        response.raise_for_status()
        return {"email": response.json().get("email")}
    except (CalendarNotConnectedError, ValueError):
        return {"email": None}
    except Exception as error:
        logger.warning("Could not retrieve connected Google account (%s).", type(error).__name__)
        return {"email": None}


class PermissionChange(BaseModel):
    service: Literal["calendar_read", "calendar_write", "email", "email_send"]
    enabled: bool


@app.post("/api/permissions")
def change_permission(change: PermissionChange) -> dict[str, object]:
    """Toggle an integration or return a consent URL when Google scope is missing."""
    scopes = {
        "calendar_read": CALENDAR_SCOPES,
        "calendar_write": CALENDAR_WRITE_SCOPES,
        "email": GMAIL_SCOPES,
        "email_send": GMAIL_WRITE_SCOPES,
    }[change.service]
    if not change.enabled:
        set_integration_permission(change.service, False)
        return {"service": change.service, "enabled": False, "authorization_url": None}
    if google_token_has_scopes(scopes):
        set_integration_permission(change.service, True)
        return {"service": change.service, "enabled": True, "authorization_url": None}
    try:
        return {
            "service": change.service,
            "enabled": False,
            "authorization_url": start_google_oauth(change.service),
        }
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/auth/google/start")
def google_auth_start(service: str = "all") -> dict[str, str]:
    """Create a Google consent URL for the requested read-only integrations."""
    try:
        return {"authorization_url": start_google_oauth(service)}
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/auth/google/callback", response_class=HTMLResponse)
def google_auth_callback(request: Request) -> HTMLResponse:
    """Exchange Google's one-time callback code and save its token privately."""
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(_oauth_popup_html(False, "Google authorization was not completed."), status_code=400)

    try:
        service = complete_google_oauth(str(request.url), request.query_params.get("state", ""))
    except ValueError as error:
        return HTMLResponse(_oauth_popup_html(False, str(error)), status_code=400)

    enabled_services = (
        ["calendar_read", "calendar_write", "email"]
        if service == "all"
        else [service]
    )
    service_scopes = {
        "calendar_read": CALENDAR_SCOPES,
        "calendar_write": CALENDAR_WRITE_SCOPES,
        "email": GMAIL_SCOPES,
        "email_send": GMAIL_WRITE_SCOPES,
    }
    for enabled_service in enabled_services:
        if google_token_has_scopes(service_scopes[enabled_service]):
            set_integration_permission(enabled_service, True)
    return HTMLResponse(_oauth_popup_html(True, "Google access is connected."))


def _oauth_popup_html(ok: bool, message: str) -> str:
    """Notify the dashboard that consent completed, then close the popup."""
    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>Google access</title></head>
<body style=\"font:16px system-ui;padding:2rem;background:#10151b;color:#eef3f7\"><p>{safe_message}</p>
<script>if(window.opener){{window.opener.postMessage({{type:'google-oauth-complete',ok:{str(ok).lower()}}},'*');window.close();}}</script></body></html>"""


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Handle one user message; sync SDK work runs in FastAPI's thread pool."""
    try:
        answer = run_agent(get_model_client(), request.message)
    except APIError as error:
        _log_model_error(error)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not reach its model provider.",
        ) from error
    except RuntimeError as error:
        logger.error("Agent loop stopped (%s).", type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not complete this request.",
        ) from error

    return ChatResponse(answer=answer)


@app.post("/api/ask", response_model=ChatResponse)
def ask_all_agents(request: ChatRequest) -> ChatResponse:
    """Answer in a persistent thread, routing explicit calendar writes safely."""
    client = get_model_client()
    conversation_id = request.conversation_id
    if conversation_id is None:
        conversation_id = create_conversation()["id"]
    elif get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    history = get_model_history(conversation_id)

    if is_calendar_write_request(request.message):
        try:
            service = get_google_service(CALENDAR_WRITE_SCOPES, "Calendar event write")
            email_service = None
            if re.search(r"\b(email|emails|gmail|inbox|message)\b", request.message, re.IGNORECASE):
                email_service = get_google_service(GMAIL_SCOPES, "Gmail")
            answer = run_calendar_writer(
                client, request.message, service, email_service, history
            )
        except CalendarNotConnectedError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except APIError as error:
            _log_model_error(error)
            raise HTTPException(
                status_code=502,
                detail="The assistant could not reach its model provider.",
            ) from error
        except RuntimeError as error:
            logger.error("Calendar writer stopped: %s", error)
            raise HTTPException(
                status_code=502,
                detail="The assistant could not complete the calendar request.",
            ) from error
        save_turn(conversation_id, request.message, answer)
        return ChatResponse(answer=answer, conversation_id=conversation_id)

    def calendar_answer() -> str:
        return run_calendar_agent(
            client, request.message, get_calendar_service(), history
        )

    def email_answer() -> str:
        service, write_factory = _gmail_services_for_agent()
        return run_email_agent(
            client,
            request.message,
            service,
            history,
            write_factory,
        )

    tasks = [
        ("general", run_agent, (client, request.message, history)),
        ("calendar", calendar_answer, ()),
        ("email", email_answer, ()),
    ]
    reports: list[dict[str, str]] = []
    successful_reports: list[dict[str, str]] = []
    successful_answers = 0
    email_processing_error: str | None = None
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(function, *arguments) for _, function, arguments in tasks]
        for (name, _, _), future in zip(tasks, futures):
            try:
                answer = future.result()
                successful_answers += 1
                successful_reports.append({"agent": name, "answer": answer})
            except CalendarNotConnectedError as error:
                answer = str(error)
            except ValueError as error:
                answer = str(error)
            except APIError as error:
                _log_model_error(error)
                if name == "email":
                    status = getattr(error, "status_code", None)
                    if status == 413:
                        answer = (
                            "Gmail access is active, but Groq rejected the email-agent request as too large (HTTP 413). "
                            "Try searching one date at a time or adding a sender or subject keyword."
                        )
                    elif status == 429:
                        answer = (
                            "Gmail access is active, but Groq rate-limited the email answer. "
                            "Wait briefly and retry the search."
                        )
                    else:
                        answer = (
                            "Gmail access is active, but the model provider could not finish processing the email search."
                        )
                    email_processing_error = answer
                else:
                    answer = "The model provider could not answer this part of the question."
            except RuntimeError as error:
                logger.error("%s agent stopped (%s).", name, type(error).__name__)
                if name == "email":
                    answer = (
                        "Gmail access is active, but the email agent could not finish processing the search results. "
                        "Try searching one date at a time or adding a sender or subject keyword."
                    )
                    email_processing_error = answer
                else:
                    answer = "This part of the question could not be answered."
            reports.append({"agent": name, "answer": answer})

    if email_processing_error and re.search(
        r"\b(email|emails|gmail|inbox|message|messages)\b", request.message, re.IGNORECASE
    ):
        # Don't let answer synthesis mislabel an LLM payload/rate-limit error as
        # missing Gmail permission. The Gmail permission check already succeeded.
        save_turn(conversation_id, request.message, email_processing_error)
        return ChatResponse(answer=email_processing_error, conversation_id=conversation_id)

    email_requested = re.search(
        r"\b(email|emails|gmail|inbox|message|messages)\b", request.message, re.IGNORECASE
    )
    calendar_requested = re.search(
        r"\b(calendar|event|meeting|appointment|schedule)\b", request.message, re.IGNORECASE
    )
    if email_requested and not calendar_requested:
        email_report = next(
            (report for report in reports if report["agent"] == "email"), None
        )
        if email_report:
            # Email answers are already formatted by their specialist. Avoid
            # passing old contradictory chat turns through another model call.
            answer = email_report["answer"]
            save_turn(conversation_id, request.message, answer)
            return ChatResponse(answer=answer, conversation_id=conversation_id)

    if successful_answers == 0:
        raise HTTPException(
            status_code=502,
            detail="The assistant could not answer this question. Check the app logs and try again.",
        )

    try:
        answer = synthesize_agent_answers(client, request.message, reports)
    except APIError as error:
        _log_model_error(error)
        answer = _fallback_answer(successful_reports, len(reports) - len(successful_reports))
    except RuntimeError as error:
        logger.error("Answer synthesis failed: %s", error)
        answer = _fallback_answer(successful_reports, len(reports) - len(successful_reports))

    save_turn(conversation_id, request.message, answer)
    return ChatResponse(answer=answer, conversation_id=conversation_id)


@app.post("/api/calendar/chat", response_model=ChatResponse)
def calendar_chat(request: ChatRequest) -> ChatResponse:
    """Handle a request with the specialist's Calendar-only tools."""
    try:
        service = get_calendar_service()
        answer = run_calendar_agent(get_model_client(), request.message, service)
    except CalendarNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        logger.error("Calendar agent configuration failed (%s).", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="Calendar agent configuration is invalid.",
        ) from error
    except APIError as error:
        _log_model_error(error)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not reach its model provider.",
        ) from error
    except RuntimeError as error:
        logger.error("Calendar agent loop stopped (%s).", type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail="The Calendar Agent could not complete this request.",
        ) from error

    return ChatResponse(answer=answer)


@app.post("/api/calendar/events")
def calendar_event_create(request: CalendarEventRequest) -> dict[str, object]:
    """Create one event on the primary calendar, without inviting anyone."""
    try:
        service = get_google_service(CALENDAR_WRITE_SCOPES, "Calendar event write")
        return create_calendar_event(
            service,
            request.summary,
            request.start,
            request.end,
            request.time_zone,
            request.description,
        )
    except CalendarNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        logger.warning("Calendar event creation was rejected (%s).", type(error).__name__)
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/email/chat", response_model=ChatResponse)
def email_chat(request: ChatRequest) -> ChatResponse:
    """Handle a request with the read-only Gmail search capability."""
    try:
        service, write_factory = _gmail_services_for_agent()
        answer = run_email_agent(
            get_model_client(), request.message, service, None, write_factory
        )
    except CalendarNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        logger.error("Email integration request failed (%s).", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="Gmail access is unavailable. Check the Google connection and Gmail API setup.",
        ) from error
    except APIError as error:
        _log_model_error(error)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not reach its model provider.",
        ) from error
    except RuntimeError as error:
        logger.error("Email assistant loop stopped (%s).", type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not complete this request.",
        ) from error

    return ChatResponse(answer=answer)
