# Learning Notes

## Phase 0 — Project setup

- **Concept:** A project scaffold separates source code, user interface, private data, and configuration.
- **Why here:** The API must later serve both the web dashboard and a possible mobile client; private documents and credentials must stay out of source control.
- **When to use:** Starting a Python service; separating a web client from an API; keeping local secrets and user data out of commits.
- **Gotchas:** A virtual environment uses the Python version that created it; `.gitignore` does not untrack files already committed; a sample env file must not contain real secrets.
- **Files touched:** `.gitignore`, `.dockerignore`, `.env.example`, `Dockerfile`, `compose.yaml`, `pyproject.toml`, `README.md`, `LEARNING.md`, and the empty source directories.
- **Docker:** Use `Dockerfile` and `compose.yaml` to run Python 3.11 in an isolated container; source is mounted from the host for editing. `.dockerignore` keeps secrets and private data out of the build context.
- **Verification:** `docker compose config` parsed successfully; `docker compose build` completed; `docker compose run --rm app` printed `Python 3.11.17`.
- **Gotcha:** A Docker container is isolated from the host, but its bind mount makes files in the project directory visible to the app. The Docker daemon must be running before Compose can build or start containers.
- **Environment note:** The host Python is 3.8.10, but Docker and Compose provide the project's Python 3.11 runtime without changing the host installation.

## Phase 1 — API and hand-written agent loop

- **Concept:** A vertical slice implements one real feature end to end. The API passes a user message to the configured model, runs an allowed tool if requested, then returns its answer.
- **Why here:** It establishes the reusable backend boundary and the model/tool protocol used by future specialist agents.
- **When to use:** Building the first real user workflow; sharing one backend between web and mobile clients; constraining model actions with explicit tool allowlists.
- **Gotchas:** `tool_use` is a request, not a result; pair each result with its `tool_use_id`; stop after a fixed number of tool rounds; never expose the provider key to a browser client.
- **Safety boundary:** The only tool reads the actual current time. It has no write actions. The API listens on a host loopback port; do not expose it remotely until authentication is implemented.
- **Files touched:** `backend/__init__.py`, `backend/tools.py`, `backend/agent.py`, `backend/main.py`, `Dockerfile`, `compose.yaml`, `.env.example`, `pyproject.toml`, `README.md`, and `LEARNING.md`. Removed `backend/demo.py`.
- **Verification:** Docker image built; `/health` returned `{"status":"ok"}`; an empty chat message returned HTTP 422. No paid model request was made. A valid chat request requires the user's Anthropic API key and may incur API charges.
- **Environment note:** Host port 8000 was occupied by another app, so Compose uses localhost port 8001 by default. Set `APP_PORT` to change it.

## Phase 2.1 — Google OAuth foundation

- **Concept:** OAuth lets a user grant an app limited access to an account without sharing the account password. A short-lived state value ties the authorization callback to the flow the app started.
- **Why here:** The Calendar Agent needs Google permission before it can read events. This first slice adds a safe connection path before adding any Calendar tools.
- **When to use:** Connecting an app to a user's account; delegating limited API access; requesting only the permissions a feature needs.
- **Gotchas:** Use the narrowest scope, keep tokens out of source control and browser code, register the exact redirect URI, and validate one-time OAuth state. In-memory state assumes one backend process.
- **Scope:** `calendar.events.readonly` allows reading events; no event write scope is requested.
- **Files touched:** `backend/google_oauth.py`, `backend/main.py`, `.env.example`, `pyproject.toml`, `README.md`, and `LEARNING.md`.
- **Verification:** Pending dependency installation/build and local OAuth setup; a real consent round trip requires the user's Google OAuth client credentials.

## Phase 2.2 — Read-only Calendar Agent

- **Concept:** A specialist agent is a bounded tool loop with its own instructions and tool allowlist. The Calendar Agent can ask for event data; the backend performs a read-only Calendar API request and returns a small result.
- **Why here:** It turns the OAuth connection into a useful feature while keeping Calendar access separate from the general assistant and limiting the data sent to the model.
- **When to use:** Keeping integrations independently permissioned; creating agents with different responsibilities; limiting sensitive API responses to the fields a task needs.
- **Gotchas:** RFC3339 time bounds need timezone offsets; event API results can be paginated; event titles and locations are untrusted text; distinguish primary calendar data from all calendars; do not claim availability if results are incomplete.
- **Safety boundary:** This agent has only one read-only tool. It fetches event title, start, end, location, and timezone from the primary calendar. It cannot create, update, or delete events.
- **Files touched:** `backend/agent.py`, `backend/calendar_agent.py`, `backend/calendar_tools.py`, `backend/main.py`, `pyproject.toml`, `.gitignore`, `.dockerignore`, `README.md`, and `LEARNING.md`.
- **Verification:** Compose configuration parsed and all seven Python files passed an AST syntax parse. Docker image build was blocked because access to the Docker daemon was denied. A live query still requires connected Google credentials and an Anthropic API key.

## Phase 3 — Read-only Email Agent

- **Concept:** A specialist can search Gmail through a narrow read-only tool, returning a small number of relevant messages for the assistant to summarize.
- **Why here:** It adds an email workflow while keeping all message changes unavailable and limiting the amount of mail sent to the model.
- **Safety boundary:** Uses Gmail's `gmail.readonly` OAuth scope. Search returns at most five emails and includes only sender, recipient, subject, date, and a bounded plain-text excerpt. Email content is untrusted and must not be followed as instructions. The agent cannot send or modify email.
- **OAuth:** Reconnect Google to grant Gmail access. The consent flow requests both the existing Calendar scope and Gmail read-only scope. Google classifies `gmail.readonly` as restricted; wider distribution may require verification.
- **Files touched:** `backend/email_agent.py`, `backend/email_tools.py`, `backend/calendar_tools.py`, `backend/google_oauth.py`, `backend/main.py`, `frontend/index.html`, `README.md`, and `LEARNING.md`.
- **Verification:** Pending local Google consent for the Gmail scope and a user initiated email query.

## Catch-up note — what Phases 2–3 grew into

- The notes above describe the first slices. The code has since moved on: the model provider is Groq (OpenAI-compatible), not Anthropic; Calendar can create events (`calendar_writer.py`, scope `calendar.events`); Gmail can create, edit and send drafts (scope `gmail.compose`) behind a separate "Manage drafts and send email" permission; chats persist in SQLite (`conversations.py`); `/api/ask` runs the general, calendar and email agents in parallel and merges their answers; permissions are toggled per service from the dashboard.

## Phase 4 — Reply drafts for incoming email

- **Concept:** Let the model decide *what to say*, but let the backend decide *who it goes to*. The new `create_reply_draft` tool takes only a `message_id` and a body. The backend re-fetches that message and derives the recipient (`Reply-To`, else `From`), the `Re:` subject, and the `In-Reply-To`/`References` headers plus Gmail `threadId`, so the draft lands in the original conversation.
- **Why here:** Drafting reply text from text the model read in an email is a prompt-injection path. If the model supplied the recipient, a malicious email could say "reply to attacker@…". Taking the address from Gmail's own headers closes that.
- **When to use:** Any tool where untrusted content could steer a side effect — keep the side effect's targets out of the model's hands and derive them from trusted data.
- **Gotchas:** Threading needs both the `threadId` and matching `Message-ID` headers, and Gmail ignores `threadId` if the subject differs. The tool only accepts IDs that `search_gmail` returned in the same request, is capped at 3 drafts per request, and is idempotent per message. Drafts only: sending still needs a separate, explicit request.
- **Files touched:** `backend/email_tools.py` (search now returns `message_id`/`thread_id`; new `create_reply_draft`), `backend/email_agent.py` (new tool, prompt rules, per-request guards), `tests/test_reply_draft.py`, `pyproject.toml`, `README.md`, `LEARNING.md`.
- **Verification:** 4 unit tests with a fake Gmail service pass in the container (`docker compose run --rm app sh -c "pip install -q pytest && python -m pytest tests"`). A live end-to-end run needs your Google connection and Groq key.

## Phase 5 — Voice: speech-to-text and read-aloud

- **Concept:** Two different directions with different tools. *Dictation* (audio → text) needs a model, so the browser records with `MediaRecorder`, posts the raw audio to `/api/transcribe`, and the backend calls Groq's Whisper (`whisper-large-v3-turbo`, override with `GROQ_STT_MODEL`). *Read aloud* (text → audio) uses the browser's built-in `speechSynthesis`, so it needs no key, server code or network.
- **Why here:** It reuses the existing Groq key and keeps the API key out of the browser. The transcript goes into the text box instead of being sent, so you can fix mistakes before the assistant acts on them — important because the assistant can create drafts and events.
- **Gotchas:** Browsers only allow the microphone on `localhost` or HTTPS. Recorders emit `audio/webm;codecs=opus` (Chrome/Firefox) or `audio/mp4` (Safari), so the backend ignores codec parameters and allowlists types. Uploads are capped at 10 MB. Audio leaves your machine for Groq; read-aloud voices depend on the OS/browser. The raw-body endpoint avoids needing `python-multipart`.
- **Files touched:** `backend/speech.py`, `backend/main.py` (`POST /api/transcribe`), `frontend/index.html` (🎙 Speak button, 🔊 Read aloud action), `tests/test_speech.py`, `LEARNING.md`.
- **Verification:** 8 tests pass in the container; the endpoint rejects a non-audio type with HTTP 400 and reaches Groq for audio-typed data. Actual microphone capture needs a browser — not tested by me.
