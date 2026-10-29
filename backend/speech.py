"""Speech-to-text through Groq's Whisper endpoint."""

import io
import os

from openai import OpenAI

MAX_AUDIO_BYTES = 10 * 1024 * 1024
AUDIO_TYPES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/x-m4a": "m4a",
}


def audio_extension(content_type: str | None) -> str:
    """Map a request Content-Type (ignoring codec parameters) to a file extension."""
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in AUDIO_TYPES:
        raise ValueError("Unsupported audio format. Use webm, ogg, mp4, mp3, wav or m4a.")
    return AUDIO_TYPES[base]


def transcribe_audio(client: OpenAI, audio: bytes, content_type: str | None) -> str:
    """Return the transcript of one short recording."""
    extension = audio_extension(content_type)
    if not audio:
        raise ValueError("No audio was received.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Recording is too large; keep it under about 10 MB.")
    result = client.audio.transcriptions.create(
        model=os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
        file=(f"recording.{extension}", io.BytesIO(audio)),
        response_format="json",
        temperature=0,
    )
    return (result.text or "").strip()
