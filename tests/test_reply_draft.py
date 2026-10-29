import base64
from email import message_from_bytes

import pytest

from backend.email_tools import create_reply_draft, search_gmail


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeGmail:
    def __init__(self, headers, thread_id="t1"):
        self.headers = headers
        self.thread_id = thread_id
        self.posted = None

    def get(self, url, params=None, timeout=None):
        if url.endswith("/messages"):
            return FakeResponse({"messages": [{"id": "m1"}]})
        payload = {"headers": [{"name": k, "value": v} for k, v in self.headers.items()]}
        return FakeResponse({"id": "m1", "threadId": self.thread_id, "payload": payload})

    def post(self, url, json=None, timeout=None):
        self.posted = (url, json)
        return FakeResponse({"id": "d1"})


def _sent_message(gmail):
    raw = gmail.posted[1]["message"]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw + "=="))


def test_reply_is_threaded_and_addressed_from_original():
    gmail = FakeGmail(
        {
            "From": "Ana <ana@example.com>",
            "Subject": "Lunch?",
            "Message-ID": "<abc@mail>",
            "References": "<root@mail>",
        }
    )
    result = create_reply_draft(gmail, "m1", "Sure, noon works.")
    msg = _sent_message(gmail)
    assert gmail.posted[0].endswith("/drafts")
    assert gmail.posted[1]["message"]["threadId"] == "t1"
    assert msg["To"] == "ana@example.com"
    assert msg["Subject"] == "Re: Lunch?"
    assert msg["In-Reply-To"] == "<abc@mail>"
    assert msg["References"] == "<root@mail> <abc@mail>"
    assert result["created"] and result["to"] == "ana@example.com"


def test_reply_prefers_reply_to_and_does_not_double_prefix():
    gmail = FakeGmail(
        {"From": "a@example.com", "Reply-To": "b@example.com", "Subject": "Re: Hi"}
    )
    create_reply_draft(gmail, "m1", "ok")
    msg = _sent_message(gmail)
    assert msg["To"] == "b@example.com"
    assert msg["Subject"] == "Re: Hi"


def test_reply_rejects_bad_message_id_and_never_sends():
    gmail = FakeGmail({"From": "a@example.com", "Subject": "x"})
    with pytest.raises(ValueError):
        create_reply_draft(gmail, "../messages/send", "hi")
    assert gmail.posted is None


def test_search_exposes_ids_for_replying():
    gmail = FakeGmail({"From": "a@example.com", "Subject": "x"})
    result = search_gmail(gmail, "is:unread")
    assert result["emails"][0]["message_id"] == "m1"
    assert result["emails"][0]["thread_id"] == "t1"
