"""Read-only Google service access tools for the assistant integrations."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
import requests

from backend.conversations import get_integration_permission
from backend.google_oauth import CALENDAR_SCOPES, load_token_info, save_token

logger = logging.getLogger(__name__)


class GoogleIntegrationNotConnectedError(RuntimeError):
    """Raised when Google OAuth lacks a requested integration permission."""


# Keep the existing Calendar API exception name for its current callers.
CalendarNotConnectedError = GoogleIntegrationNotConnectedError


def get_google_service(required_scopes: list[str], service_name: str) -> Any:
    """Load or refresh saved Google credentials for one read-only integration."""
    integration = {
        "Calendar": "calendar_read",
        "Calendar event write": "calendar_write",
        "Gmail": "email",
        "Gmail write": "email_send",
    }.get(service_name)
    if integration and not get_integration_permission(integration):
        raise CalendarNotConnectedError(
            f"Google {service_name} access is turned off in Assistant permissions."
        )
    info = load_token_info()
    if info is None:
        raise CalendarNotConnectedError(
            f"Google {service_name} is not connected. Complete Google sign-in first."
        )

    try:
        credentials = Credentials.from_authorized_user_info(info)
    except ValueError as error:
        raise CalendarNotConnectedError(
            "Saved Google credentials could not be read. Connect again."
        ) from error
    if not credentials.has_scopes(required_scopes):
        raise CalendarNotConnectedError(
            f"Google {service_name} read access is missing. Reconnect Google and approve the requested access."
        )

    if credentials.expired:
        if not credentials.refresh_token:
            raise CalendarNotConnectedError(
                f"Google {service_name} access expired. Reconnect Google."
            )
        try:
            credentials.refresh(Request())
        except GoogleAuthError as error:
            raise CalendarNotConnectedError(
                f"Google {service_name} access could not be refreshed. Reconnect Google."
            ) from error
        save_token(credentials.to_json())

    if not credentials.valid:
        raise CalendarNotConnectedError(
            f"Google {service_name} credentials are not valid. Reconnect Google."
        )

    return AuthorizedSession(credentials)


def google_token_has_scopes(required_scopes: list[str]) -> bool:
    """Check granted scopes from the saved token without exposing token contents."""
    info = load_token_info()
    if info is None:
        return False
    try:
        credentials = Credentials.from_authorized_user_info(info)
    except ValueError:
        return False
    return credentials.has_scopes(required_scopes)


def get_calendar_service() -> Any:
    """Load Google credentials with read-only access to Calendar events."""
    return get_google_service(CALENDAR_SCOPES, "Calendar")


def create_calendar_event(
    service: Any,
    summary: str,
    start: str,
    end: str,
    time_zone: str,
    description: str = "",
) -> dict[str, Any]:
    """Create a primary-calendar event without inviting or notifying attendees."""
    if not summary.strip() or len(summary) > 200:
        raise ValueError("Event title must be between 1 and 200 characters.")
    if len(description) > 4000:
        raise ValueError("Event description must be at most 4000 characters.")
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        ZoneInfo(time_zone)
    except (ValueError, TypeError) as error:
        raise ValueError("Use valid RFC3339 start/end times and an IANA timezone.") from error
    if start_time.utcoffset() is None or end_time.utcoffset() is None:
        raise ValueError("Event start and end must include a timezone offset.")
    if end_time <= start_time:
        raise ValueError("Event end must be later than its start.")

    # Make repeated submissions idempotent for the same title and exact start.
    existing = list_calendar_events(service, start, end, max_results=25)
    start_instant = start_time.astimezone(timezone.utc)
    if any(
        event.get("title", "").strip().casefold() == summary.strip().casefold()
        and event.get("start")
        and datetime.fromisoformat(event["start"].replace("Z", "+00:00")).astimezone(timezone.utc)
        == start_instant
        for event in existing["events"]
    ):
        return {"created": False, "reason": "An event with this title and start time already exists."}

    payload: dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start, "timeZone": time_zone},
        "end": {"dateTime": end, "timeZone": time_zone},
    }
    if description.strip():
        payload["description"] = description.strip()

    try:
        response = service.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            params={"sendUpdates": "none"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        logger.warning("Google Calendar event creation failed (HTTP %s).", status or "unknown")
        raise ValueError("Google Calendar could not create the event. Check write access and try again.") from error

    event = response.json()
    return {
        "created": True,
        "id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "summary": event.get("summary"),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
    }


def update_calendar_event(
    service: Any,
    event_id: str,
    start: str,
    end: str,
    time_zone: str,
) -> dict[str, Any]:
    """Reschedule a specific primary-calendar event without changing its other fields."""
    if not event_id or len(event_id) > 512:
        raise ValueError("A valid Calendar event ID is required.")
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        ZoneInfo(time_zone)
    except (ValueError, TypeError) as error:
        raise ValueError("Use valid RFC3339 start/end times and an IANA timezone.") from error
    if start_time.utcoffset() is None or end_time.utcoffset() is None:
        raise ValueError("Event start and end must include a timezone offset.")
    if end_time <= start_time:
        raise ValueError("Event end must be later than its start.")

    try:
        response = service.patch(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            params={"sendUpdates": "none"},
            json={
                "start": {"dateTime": start, "timeZone": time_zone},
                "end": {"dateTime": end, "timeZone": time_zone},
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        logger.warning("Google Calendar event update failed (HTTP %s).", status or "unknown")
        raise ValueError("Google Calendar could not update the event. Check write access and try again.") from error

    event = response.json()
    return {
        "updated": True,
        "id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "summary": event.get("summary"),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
    }


def list_calendar_events(
    service: Any,
    time_min: str,
    time_max: str,
    max_results: int = 20,
) -> dict[str, Any]:
    """List a bounded window of events, returning only scheduling details."""
    try:
        start = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
        end = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Use RFC3339 timestamps with a timezone offset for both bounds.") from error

    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Both time bounds must include a timezone offset, such as Z.")
    if end <= start:
        raise ValueError("time_max must be later than time_min.")
    if not 1 <= max_results <= 25:
        raise ValueError("max_results must be between 1 and 25.")

    try:
        response = service.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": max_results,
                "singleEvents": "true",
                "orderBy": "startTime",
                "fields": "timeZone,nextPageToken,items(id,summary,start,end,location,status)",
            },
            timeout=30,
        )
        response.raise_for_status()
        calendar_data = response.json()
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status:
            reason = None
            try:
                payload = response.json()
                api_error = payload.get("error", {})
                errors = api_error.get("errors", [])
                reason = errors[0].get("reason") if errors else api_error.get("status")
            except (AttributeError, ValueError, TypeError):
                pass

            logger.warning(
                "Google Calendar API request failed (HTTP %s, reason=%s).",
                status,
                reason or "unknown",
            )

            if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
                raise ValueError(
                    "Google Calendar API is disabled in the Google Cloud project for this OAuth app. Enable the Google Calendar API, then retry."
                ) from error
            if reason == "insufficientPermissions":
                raise ValueError(
                    "Google Calendar denied the current token's permissions. Reconnect Google Calendar and approve calendar event read access."
                ) from error
            if reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
                raise ValueError(
                    "Google Calendar rate limit was reached. Wait briefly and retry."
                ) from error

            raise ValueError(
                f"Google Calendar returned HTTP {status} (reason: {reason or 'unknown'}); check that the Calendar API is enabled and this account has event read access."
            ) from error
        logger.warning(
            "Google Calendar API request failed (%s).", type(error).__name__
        )
        raise ValueError(
            "Could not reach Google Calendar; check the connection and try again."
        ) from error

    events = []
    for event in calendar_data.get("items", []):
        if event.get("status") == "cancelled":
            continue
        events.append(
            {
                "id": event.get("id"),
                "title": event.get("summary", "(untitled event)"),
                "start": event.get("start", {}).get("dateTime")
                or event.get("start", {}).get("date"),
                "end": event.get("end", {}).get("dateTime")
                or event.get("end", {}).get("date"),
                "location": event.get("location"),
            }
        )

    return {
        "time_zone": calendar_data.get("timeZone"),
        "events": events,
        "count": len(events),
        "has_more": bool(calendar_data.get("nextPageToken")),
    }
