---
title: Personal Assistant Dashboard
emoji: 🗓️
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 8000
pinned: false
---

# Personal Assistant Dashboard

A personal learning project: a Python API with a simple browser interface, built in small phases.

## Setup

This project uses Docker's official Python 3.11 image, so you do not need to install Python 3.11 on your host. Install Docker Desktop (Windows/macOS) or Docker Engine plus the Compose plugin (Linux), and make sure the Docker daemon is running.

```bash
git init
if [ ! -f .env ]; then cp .env.example .env; fi
docker compose build
```

Create `.env` from `.env.example` if it does not already exist. Keep real credentials in `.env`; `.gitignore` and `.dockerignore` keep it out of Git and the image build context. Set `APP_TIMEZONE` to an IANA timezone name for the current-time tool.

## Planned structure

- `backend/`: API and agent implementation, kept separate from the browser so a mobile client can reuse the API.
- `frontend/`: the browser dashboard for asking questions and viewing one concise answer.
- `docs/`: project and learning notes.
- `data/`: private local files and indexes; ignored by Git.
- `LEARNING.md`: short notes from each completed learning step.

## Run the API

Start the API locally:

```bash
docker compose build
docker compose up
```

The health endpoint is available at `http://127.0.0.1:8001/health`. Add a Groq API key to `.env` as `GROQ_API_KEY=...` to enable chat. Create a key in the [Groq Console](https://console.groq.com/). The default model is `openai/gpt-oss-120b`; change it with `GROQ_MODEL`. Groq's free tier has per-model request and token limits. If port 8001 is already in use, set `APP_PORT` in `.env` to an available port.

Open the browser dashboard at `http://127.0.0.1:8001/`. It uses the current assistant capabilities together and displays one combined answer. The dashboard is also available after you start Compose with `docker compose up`.

## Saved chat threads

The dashboard keeps separate chat threads in a local SQLite database at `data/chat_history.sqlite3`. You can create a chat, switch between saved chats, reload the page, or delete a chat. Each request sends its conversation ID; the backend stores user/assistant turns and gives the recent history to the agents so follow-up questions have context. The visible thread is retained in full, while each model call receives up to the latest 16 messages, capped at 16,000 characters. The database is in the ignored `data/` directory and stays on the host through the Compose bind mount. This local app does not yet authenticate users or isolate separate users' threads, so keep it bound to localhost.

```bash
curl -X POST http://127.0.0.1:8001/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What time is it?"}'
```

The API uses Groq's OpenAI-compatible API. Its free tier has usage limits; upgrading to a paid tier can incur charges. The assistant can answer general questions using the current-time tool and connected Google services. The host port is bound to localhost; do not expose this API to a network until authentication is added.

## Connect Google Calendar (Phase 2)

This local OAuth setup requests identity access to display the connected Google email, read access to Calendar events and Gmail, Calendar event-write access, and Gmail send access when enabled. In Google Cloud, enable the Google Calendar and Gmail APIs, configure the OAuth consent screen for testing, and create an OAuth client of type **Web application**. Add `http://127.0.0.1:8001/auth/google/callback` as an authorized redirect URI, then put the client ID and secret in `.env` (never in source files). Add the scopes used by the app to Data Access, including `https://www.googleapis.com/auth/calendar.events` for event creation, `https://www.googleapis.com/auth/userinfo.email` for account display, and `https://www.googleapis.com/auth/gmail.compose` to allow the assistant to save drafts and send mail.

After restarting Compose, open `http://127.0.0.1:8001/auth/google/start`, copy the `authorization_url` from the JSON response into a browser, and approve access. Google returns to the callback and the backend stores its token in `data/google_calendar_token.json`, which is ignored by Git. If you change `APP_PORT`, also update `GOOGLE_REDIRECT_URI` in `.env` and Google Cloud to use that port.

The Calendar Agent endpoint is `POST http://127.0.0.1:8001/api/calendar/chat`. For example:

```bash
curl -X POST http://127.0.0.1:8001/api/calendar/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What events do I have tomorrow?"}'
```

The Calendar Agent endpoint reads only the primary calendar and returns event title, time, and location. In dashboard chat (`POST /api/ask`), explicit requests to create, add, schedule, or book a calendar event use the write-enabled Calendar tool. A missing duration defaults to one hour; unclear date, time, or title triggers a clarification. Matching title/start events are skipped to avoid duplicates. Explicit requests to move or reschedule an existing event first search the primary calendar, then update only a single clear match while preserving its duration. The direct read-only Calendar endpoint remains `POST /api/calendar/chat`. Event titles and locations are treated as untrusted text. OAuth state is held in memory, which is suitable for this single-process local setup but will need shared storage if the service later runs multiple workers or replicas.

Tool-calling agents share a bounded dispatcher in `backend/agent.py`. Each specialist receives only its assigned tools, tool loops have a per-agent round limit, and returned tool data is capped before it is sent back to the model. Tool logs include the agent and tool names but do not log prompts, email contents, or credentials.

## Connect read-only Gmail (Phase 3)

Enable the Gmail API in the same Google Cloud project as the OAuth client. In Google Auth Platform → Data Access, add `https://www.googleapis.com/auth/gmail.readonly`; also add `https://www.googleapis.com/auth/gmail.send` if you want to enable sending. The dashboard shows the connected Google account and has separate switches for Calendar read, Calendar write, email read, and sending email. Turning on a permission opens Google's consent popup if the current token does not include its scope; turning it off blocks that integration in the app. The assistant sends only when directly asked and has the recipient, subject, and body. While the app is in testing, add your Google account as a test user if prompted. Google may require verification for broader use.

Open the dashboard and enable the desired permissions. The Email Agent searches up to five matching messages and sends short message excerpts to Groq to answer your question. If **Send email on explicit request** is enabled, it can send a plain-text message only when directly instructed and given a recipient, subject, and body. It cannot delete, archive, label, or edit existing messages. Its direct endpoint is `POST http://127.0.0.1:8001/api/email/chat`.

Calendar events are created only on the primary calendar, without attendees or notifications. The direct endpoint is `POST http://127.0.0.1:8001/api/calendar/events`; it requires an explicit title, RFC3339 start and end times, and an IANA timezone. Duplicate title/start combinations are skipped.

## Reply drafts

With **Manage drafts and send email** enabled, ask the assistant things like "Check my unread emails and draft replies to any that need one". It saves threaded reply drafts in Gmail Drafts (at most three per request), skips newsletters and no-reply senders, and shows you each draft. It never sends unless you separately ask. Recipients are taken from the original message, not from model output.

## Tests

```bash
docker compose run --rm app sh -c "pip install -q pytest && python -m pytest tests"
```
