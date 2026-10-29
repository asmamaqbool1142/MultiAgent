"""Read-only Gmail search used by the Email Agent."""

import base64
from email.message import EmailMessage
import html
import logging
import re
from email.utils import parseaddr
from typing import Any

import requests

logger = logging.getLogger(__name__)
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_EMAILS = 5
MAX_BODY_CHARS = 1200


def _header_value(headers: dict[str, str], name: str, limit: int) -> str:
    """Keep unusually large headers from overflowing the model context."""
    return headers.get(name, "")[:limit]


def _decode_body(payload: dict[str, Any]) -> str:
    """Extract bounded plain text from a Gmail message payload."""
    plain_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        if part.get("mimeType") == "text/plain":
            encoded = part.get("body", {}).get("data")
            if encoded:
                try:
                    plain_parts.append(
                        base64.urlsafe_b64decode(encoded + "==").decode(
                            "utf-8", errors="replace"
                        )
                    )
                except (ValueError, TypeError):
                    pass
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    if plain_parts:
        return "\n".join(plain_parts)[:MAX_BODY_CHARS]

    # Some messages contain only HTML. Strip markup before passing a short excerpt
    # to the model; the message content is still treated as untrusted input.
    html_parts: list[str] = []

    def visit_html(part: dict[str, Any]) -> None:
        if part.get("mimeType") == "text/html":
            encoded = part.get("body", {}).get("data")
            if encoded:
                try:
                    html_parts.append(
                        base64.urlsafe_b64decode(encoded + "==").decode(
                            "utf-8", errors="replace"
                        )
                    )
                except (ValueError, TypeError):
                    pass
        for child in part.get("parts", []):
            visit_html(child)

    visit_html(payload)
    markup = " ".join(html_parts)
    return html.unescape(re.sub(r"<[^>]*>", " ", markup))[:MAX_BODY_CHARS]


def search_gmail(service: Any, query: str, max_results: int = 5) -> dict[str, Any]:
    """Search Gmail and return a small number of bounded message excerpts."""
    if not query or len(query) > 500:
        raise ValueError("Provide a Gmail search query of at most 500 characters.")
    if not 1 <= max_results <= MAX_EMAILS:
        raise ValueError(f"max_results must be between 1 and {MAX_EMAILS}.")

    try:
        response = service.get(
            f"{GMAIL_API}/messages",
            params={"q": query, "maxResults": max_results},
            timeout=30,
        )
        response.raise_for_status()
        search_result = response.json()
        message_ids = [item["id"] for item in search_result.get("messages", [])]
        emails = []
        for message_id in message_ids:
            message_response = service.get(
                f"{GMAIL_API}/messages/{message_id}",
                params={"format": "full"},
                timeout=30,
            )
            message_response.raise_for_status()
            message = message_response.json()
            headers = {
                header.get("name", "").lower(): header.get("value", "")
                for header in message.get("payload", {}).get("headers", [])
            }
            emails.append(
                {
                    "from": _header_value(headers, "from", 250),
                    "to": _header_value(headers, "to", 250),
                    "subject": _header_value(headers, "subject", 250) or "(no subject)",
                    "date": _header_value(headers, "date", 100),
                    "body_excerpt": _decode_body(message.get("payload", {})),
                }
            )
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        reason = None
        if response is not None:
            try:
                reason = response.json().get("error", {}).get("status")
            except (AttributeError, ValueError, TypeError):
                pass
        logger.warning(
            "Gmail API request failed (HTTP %s, reason=%s).",
            status or "unknown",
            reason or "unknown",
        )
        if status == 403:
            raise ValueError(
                "Google denied Gmail read access. Reconnect Google and approve Gmail read access."
            ) from error
        if status == 404:
            raise ValueError("A Gmail search result was no longer available; try again.") from error
        raise ValueError("Could not search Gmail; check the connection and try again.") from error

    return {
        "count": len(emails),
        "has_more": bool(search_result.get("nextPageToken")),
        "emails": emails,
    }


def send_gmail_message(
    service: Any, recipient: str, subject: str, body: str
) -> dict[str, Any]:
    """Send a plain text email only after the agent receives an explicit send request."""
    validate_gmail_message(recipient, subject, body)

    raw = _raw_message(recipient, subject, body)
    try:
        response = service.post(
            f"{GMAIL_API}/messages/send",
            json={"raw": raw},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        logger.warning("Gmail send failed (HTTP %s).", status or "unknown")
        if status == 403:
            raise ValueError(
                "Google denied Gmail send access. Enable the Send email permission and approve Gmail send access."
            ) from error
        raise ValueError("Gmail could not confirm sending this message. Check Sent before retrying.") from error

    sent = response.json()
    return {"sent": True, "id": sent.get("id"), "thread_id": sent.get("threadId")}


def create_gmail_draft(
    service: Any, recipient: str, subject: str, body: str
) -> dict[str, Any]:
    """Create a Gmail draft without sending it."""
    raw = _raw_message(recipient, subject, body)
    try:
        response = service.post(
            f"{GMAIL_API}/drafts",
            json={"message": {"raw": raw}},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        _raise_gmail_write_error(error, "create the draft")
    draft = response.json()
    return {
        "created": True,
        "draft_id": draft.get("id"),
        "message_id": draft.get("message", {}).get("id"),
    }


def list_gmail_drafts(
    service: Any, query: str = "", max_results: int = 5
) -> dict[str, Any]:
    """Return a bounded list of recent drafts, optionally filtered by visible fields."""
    if len(query) > 200:
        raise ValueError("Draft search text must be at most 200 characters.")
    if not 1 <= max_results <= MAX_EMAILS:
        raise ValueError(f"max_results must be between 1 and {MAX_EMAILS}.")
    try:
        response = service.get(
            f"{GMAIL_API}/drafts",
            params={"maxResults": min(20, max_results * 4)},
            timeout=30,
        )
        response.raise_for_status()
        listing = response.json()
        drafts = []
        for item in listing.get("drafts", []):
            draft_response = service.get(
                f"{GMAIL_API}/drafts/{item['id']}",
                params={"format": "full"},
                timeout=30,
            )
            draft_response.raise_for_status()
            draft = draft_response.json()
            message = draft.get("message", {})
            headers = {
                header.get("name", "").lower(): header.get("value", "")
                for header in message.get("payload", {}).get("headers", [])
            }
            record = {
                "draft_id": draft.get("id"),
                "to": _header_value(headers, "to", 250),
                "subject": _header_value(headers, "subject", 250) or "(no subject)",
                "body_excerpt": _decode_body(message.get("payload", {})),
            }
            if not query or query.casefold() in " ".join(record.values()).casefold():
                drafts.append(record)
            if len(drafts) >= max_results:
                break
    except requests.RequestException as error:
        _raise_gmail_write_error(error, "list drafts")
    return {
        "count": len(drafts),
        "has_more": bool(listing.get("nextPageToken")) or len(drafts) >= max_results,
        "drafts": drafts,
    }


def update_gmail_draft(
    service: Any, draft_id: str, recipient: str, subject: str, body: str
) -> dict[str, Any]:
    """Replace the content of one selected Gmail draft without sending it."""
    if not draft_id or len(draft_id) > 512 or "\n" in draft_id or "\r" in draft_id:
        raise ValueError("Select a valid draft from the Gmail draft list first.")
    raw = _raw_message(recipient, subject, body)
    try:
        response = service.put(
            f"{GMAIL_API}/drafts/{draft_id}",
            json={"id": draft_id, "message": {"raw": raw}},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        _raise_gmail_write_error(error, "update the draft")
    draft = response.json()
    return {"updated": True, "draft_id": draft.get("id")}


def _raw_message(recipient: str, subject: str, body: str) -> str:
    """Validate fields and encode a plain-text Gmail message."""
    validate_gmail_message(recipient, subject, body)
    _, parsed_address = parseaddr(recipient)
    message = EmailMessage()
    message["To"] = parsed_address
    message["Subject"] = subject.strip()
    message.set_content(body.strip())
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _raise_gmail_write_error(error: requests.RequestException, action: str) -> None:
    """Raise a safe Gmail write error without returning provider response content."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    logger.warning("Gmail write operation failed (HTTP %s).", status or "unknown")
    if status == 403:
        raise ValueError(
            "Google denied Gmail write access. Enable Manage drafts and send email and approve the Gmail compose permission."
        ) from error
    raise ValueError(f"Gmail could not {action}. Check the selected draft and try again.") from error


def validate_gmail_message(recipient: str, subject: str, body: str) -> None:
    """Validate user-facing fields before any outbound Gmail request is made."""
    if not recipient or len(recipient) > 320 or "\n" in recipient or "\r" in recipient:
        raise ValueError("Provide one valid recipient email address.")
    display_name, parsed_address = parseaddr(recipient)
    if display_name or parsed_address != recipient.strip() or not re.fullmatch(
        r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", parsed_address
    ):
        raise ValueError("Provide one valid recipient email address.")
    if not subject.strip() or len(subject) > 250 or "\n" in subject or "\r" in subject:
        raise ValueError("Provide a subject between 1 and 250 characters, on one line.")
    if not body.strip() or len(body) > 12000:
        raise ValueError("Provide an email body between 1 and 12,000 characters.")
