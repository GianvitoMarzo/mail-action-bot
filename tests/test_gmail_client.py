"""Gmail adapter tests, driven by a fake ``googleapiclient`` service object.

No credentials, no network: the fake mimics the shape of the Gmail API
(``users().messages().list().execute()``) closely enough to exercise the
payload decoding, the label bookkeeping and the error translation.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from bidoo_bot.adapters.gmail.client import GmailMailbox, extract_bodies
from bidoo_bot.errors import AuthError, MailboxError

HTML_BODY = "<html><body><a href='https://www.bidoo.com/x'>Riscuoti</a></body></html>"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class FakeRequest:
    def __init__(self, result: Any, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class FakeMessages:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    def list(self, **kwargs: Any) -> FakeRequest:
        self._service.list_calls.append(kwargs)
        if self._service.list_error is not None:
            return FakeRequest(None, self._service.list_error)
        return FakeRequest({"messages": [{"id": mid} for mid in self._service.messages]})

    def get(self, *, userId: str, id: str, format: str) -> FakeRequest:  # noqa: N803
        return FakeRequest(self._service.messages[id])

    def modify(self, *, userId: str, id: str, body: dict[str, Any]) -> FakeRequest:  # noqa: N803
        self._service.modified.append((id, tuple(body.get("addLabelIds", []))))
        return FakeRequest({"id": id})


class FakeLabels:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    def list(self, **_kwargs: Any) -> FakeRequest:
        return FakeRequest(
            {"labels": [{"id": lid, "name": name} for name, lid in self._service.labels.items()]}
        )

    def create(self, *, userId: str, body: dict[str, Any]) -> FakeRequest:  # noqa: N803
        name = body["name"]
        label_id = f"Label_{len(self._service.labels) + 1}"
        self._service.labels[name] = label_id
        self._service.created_labels.append(name)
        return FakeRequest({"id": label_id, "name": name})


class FakeUsers:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    def messages(self) -> FakeMessages:
        return FakeMessages(self._service)

    def labels(self) -> FakeLabels:
        return FakeLabels(self._service)

    def getProfile(self, *, userId: str) -> FakeRequest:  # noqa: N802, N803
        return FakeRequest({"emailAddress": "mario.rossi@example.invalid"})


class FakeService:
    def __init__(self, messages: dict[str, Any] | None = None) -> None:
        self.messages: dict[str, Any] = messages or {}
        self.labels: dict[str, str] = {"INBOX": "INBOX", "Bidoo": "Label_1"}
        self.created_labels: list[str] = []
        self.modified: list[tuple[str, tuple[str, ...]]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.list_error: Exception | None = None

    def users(self) -> FakeUsers:
        return FakeUsers(self)


def message_payload(
    message_id: str = "m1",
    *,
    html: str = HTML_BODY,
    text: str = "Riscuoti: https://www.bidoo.com/x",
    labels: tuple[str, ...] = ("INBOX", "Label_1"),
) -> dict[str, Any]:
    return {
        "id": message_id,
        "internalDate": "1740992100000",
        "labelIds": list(labels),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "La tua puntata gratis"},
                {"name": "From", "value": "Bidoo <noreply@example-bidoo.invalid>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Content-Type", "value": 'text/plain; charset="utf-8"'}],
                    "body": {"data": b64(text)},
                },
                {
                    "mimeType": "text/html",
                    "headers": [{"name": "Content-Type", "value": 'text/html; charset="utf-8"'}],
                    "body": {"data": b64(html)},
                },
            ],
        },
    }


# ---------------------------------------------------------------------------


def test_search_maps_payloads_to_domain_objects() -> None:
    service = FakeService({"m1": message_payload()})
    mailbox = GmailMailbox(service)

    messages = mailbox.search('label:Bidoo -label:"Bidoo/Processed"', max_results=10)

    assert len(messages) == 1
    message = messages[0]
    assert message.id == "m1"
    assert message.subject == "La tua puntata gratis"
    assert "Riscuoti" in message.html
    assert message.text.startswith("Riscuoti:")
    assert message.received_at is not None
    assert service.list_calls[0]["q"] == 'label:Bidoo -label:"Bidoo/Processed"'
    assert service.list_calls[0]["includeSpamTrash"] is False


def test_label_ids_are_resolved_to_names() -> None:
    """The core compares label *names*, so the adapter must translate them."""
    service = FakeService({"m1": message_payload(labels=("INBOX", "Label_1"))})

    messages = GmailMailbox(service).search("label:Bidoo", max_results=5)

    assert messages[0].labels == ("INBOX", "Bidoo")


def test_max_results_is_respected() -> None:
    service = FakeService({f"m{i}": message_payload(f"m{i}") for i in range(5)})

    messages = GmailMailbox(service).search("label:Bidoo", max_results=2)

    assert len(messages) == 2
    assert service.list_calls[0]["maxResults"] == 2


def test_adding_an_existing_label_does_not_create_it() -> None:
    service = FakeService({"m1": message_payload()})
    service.labels["Bidoo/Processed"] = "Label_9"

    GmailMailbox(service).add_label("m1", "Bidoo/Processed")

    assert service.modified == [("m1", ("Label_9",))]
    assert service.created_labels == []


def test_a_missing_label_is_created_once() -> None:
    service = FakeService({"m1": message_payload(), "m2": message_payload("m2")})
    mailbox = GmailMailbox(service)

    mailbox.add_label("m1", "Bidoo/Processed")
    mailbox.add_label("m2", "Bidoo/Processed")

    assert service.created_labels == ["Bidoo/Processed"]
    assert len(service.modified) == 2


def test_check_connection_redacts_the_address() -> None:
    health = GmailMailbox(FakeService()).check_connection()

    assert health.ok
    assert "mario.rossi" not in health.detail
    assert "@example.invalid" in health.detail


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def make_http_error(status: int) -> Exception:
    from googleapiclient.errors import HttpError

    class Resp:
        def __init__(self, code: int) -> None:
            self.status = code
            self.reason = "error"

    error: Exception = HttpError(Resp(status), b"{}")
    return error


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_translated(status: int) -> None:
    service = FakeService()
    service.list_error = make_http_error(status)

    with pytest.raises(AuthError, match="gmail-auth"):
        GmailMailbox(service).search("label:Bidoo", max_results=1)


def test_other_http_errors_become_mailbox_errors() -> None:
    service = FakeService()
    service.list_error = make_http_error(503)

    with pytest.raises(MailboxError, match="HTTP 503"):
        GmailMailbox(service).search("label:Bidoo", max_results=1)


def test_network_errors_become_mailbox_errors() -> None:
    service = FakeService()
    service.list_error = OSError("connection reset")

    with pytest.raises(MailboxError, match="network error"):
        GmailMailbox(service).search("label:Bidoo", max_results=1)


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------


def test_extract_bodies_handles_nested_multipart() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("testo")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "fattura.pdf",
                "body": {"data": b64("non deve essere letto")},
            },
        ],
    }

    html, text = extract_bodies(payload)

    assert html == "<p>html</p>"
    assert text == "testo"


def test_extract_bodies_handles_a_single_part_message() -> None:
    payload = {"mimeType": "text/html", "body": {"data": b64("<p>ciao</p>")}}

    html, text = extract_bodies(payload)

    assert html == "<p>ciao</p>"
    assert text == ""


def test_extract_bodies_survives_broken_base64() -> None:
    payload = {"mimeType": "text/html", "body": {"data": "!!!not base64!!!"}}

    html, _text = extract_bodies(payload)

    assert html == ""


def test_extract_bodies_honours_the_declared_charset() -> None:
    latin1 = base64.urlsafe_b64encode("perché".encode("latin-1")).decode("ascii").rstrip("=")
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="latin-1"'}],
        "body": {"data": latin1},
    }

    _html, text = extract_bodies(payload)

    assert text == "perché"
