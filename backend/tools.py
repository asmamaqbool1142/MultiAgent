"""Read-only tools exposed to the personal assistant."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_current_time() -> dict[str, str]:
    """Return the actual current time in the configured application timezone."""
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Karachi")
    try:
        now = datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f"APP_TIMEZONE must be a valid IANA timezone; got {timezone_name!r}."
        ) from error

    return {
        "timezone": timezone_name,
        "local_time": now.isoformat(timespec="seconds"),
        "weekday": now.strftime("%A"),
    }


TOOLS = [
    {
        "name": "get_current_time",
        "description": (
            "Get the actual current date and time in the application's configured timezone. "
            "Use this when the user asks what time or date it is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }
]

# Explicit dispatch keeps the model limited to tools the application exposes.
TOOL_FUNCTIONS = {"get_current_time": get_current_time}
