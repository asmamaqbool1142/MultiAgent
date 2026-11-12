import io
import zipfile

import pytest

from backend.documents import extract_text, summarize_document


def _docx(paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def test_extracts_text_and_docx():
    assert extract_text("a.txt", b"hello") == "hello"
    assert extract_text("a.docx", _docx(["One", "Two"])) == "One\nTwo"


@pytest.mark.parametrize("name,data", [("a.doc", b"x"), ("a.exe", b"x"), ("a.txt", b""), ("a.pdf", b"nope")])
def test_rejects_bad_files(name, data):
    with pytest.raises(ValueError):
        extract_text(name, data)


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        message = type("M", (), {"content": "summary"})()
        return type("R", (), {"choices": [type("C", (), {"finish_reason": "stop", "message": message})()]})()


def test_long_documents_are_condensed_in_pieces():
    client = FakeClient()
    summarize_document(client, "big.txt", ("word " * 9 + "\n") * 1000)
    assert client.calls > 2
    short = FakeClient()
    summarize_document(short, "small.txt", "tiny")
    assert short.calls == 1
