import pytest

from backend.speech import MAX_AUDIO_BYTES, audio_extension, transcribe_audio


class FakeClient:
    class audio:
        class transcriptions:
            @staticmethod
            def create(**kwargs):
                class R:
                    text = "  hello world "
                return R()


def test_codec_parameters_are_ignored():
    assert audio_extension("audio/webm;codecs=opus") == "webm"


def test_rejects_non_audio():
    with pytest.raises(ValueError):
        audio_extension("text/html")


def test_rejects_empty_and_oversized():
    with pytest.raises(ValueError):
        transcribe_audio(FakeClient, b"", "audio/webm")
    with pytest.raises(ValueError):
        transcribe_audio(FakeClient, b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm")


def test_returns_stripped_text():
    assert transcribe_audio(FakeClient, b"abc", "audio/ogg") == "hello world"
