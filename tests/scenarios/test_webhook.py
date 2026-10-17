"""Meta → webhook → router → agent → WhatsApp send, with a scripted LLM and a mocked Graph API."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.client_config import load_client_config
from app.core.clock import FixedClock
from app.main import create_app
from app.providers.llm import MockLLM
from app.runtime import Runtime
from app.settings import SecretNotFound
from app.storage.sqlite_repo import SQLiteRepository
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import parse_webhook, verify_signature

PHONE_ID = "123456789"
GRAPH = f"https://graph.facebook.com/v25.0/{PHONE_ID}/messages"
SECRET = "app-secret"
CUSTOMER = "+919847012345"
SECRETS = {"truechoice/whatsapp_token": "wa-token", "truechoice/whatsapp_app_secret": SECRET,
           "truechoice/whatsapp_verify_token": "verify-me"}


class DictSecrets:
    def get(self, name: str) -> str:
        if name not in SECRETS:
            raise SecretNotFound(name)
        return SECRETS[name]


def payload(*messages: dict, phone_id: str = PHONE_ID, statuses: list | None = None) -> dict:
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_id},
             "contacts": [{"wa_id": "919847012345", "profile": {"name": "Arun"}}],
             "messages": list(messages)}
    if statuses:
        value = {"metadata": {"phone_number_id": phone_id}, "statuses": statuses}
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(body: str, msg_id: str = "wamid.1") -> dict:
    return {"from": "919847012345", "id": msg_id, "timestamp": "1792219612", "type": "text", "text": {"body": body}}


def signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, clients_dir: Path, tmp_path: Path, clock: FixedClock):
    monkeypatch.setenv("TRUECHOICE_WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("TRUECHOICE_STAFF_NUMBERS", "")
    config = load_client_config("truechoice", clients_dir)
    repo = SQLiteRepository(tmp_path / "webhook.db")
    llm = MockLLM()
    runtime = Runtime({"truechoice": config}, repo, DictSecrets(), clock, llm=llm)
    with respx.mock(assert_all_called=False) as graph:
        sent = graph.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]}))
        yield TestClient(create_app(runtime)), llm, repo, sent
    repo.close()


def post(client: TestClient, data: dict):
    body = json.dumps(data).encode()
    return client.post("/webhook/truechoice", content=body, headers=signed(body))


def sent_bodies(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


def test_verification_handshake(env) -> None:
    client, *_ = env
    ok = client.get("/webhook/truechoice", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                   "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/webhook/truechoice", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                    "hub.challenge": "x"})
    assert bad.status_code == 403
    assert client.get("/webhook/someone-else").status_code == 404


def test_text_message_gets_agent_reply(env) -> None:
    client, llm, repo, sent = env
    llm.script.append(MockLLM.text("Namaskaram! Ethu car aanu nokkunnath?"))
    response = post(client, payload(text_msg("Hi")))
    assert response.status_code == 200 and response.json() == {"received": 1}

    read_receipt, reply = sent_bodies(sent)
    assert read_receipt == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.1",
                            "typing_indicator": {"type": "text"}}
    assert reply["to"] == "919847012345" and reply["text"]["body"] == "Namaskaram! Ethu car aanu nokkunnath?"
    assert sent.calls[1].request.headers["Authorization"] == "Bearer wa-token"
    stored = repo.list_messages("truechoice", CUSTOMER)
    assert [(m.direction, m.id if m.direction == "in" else None) for m in stored] == [("in", "wamid.1"), ("out", None)]


def test_bad_signature_is_rejected(env) -> None:
    client, llm, _, sent = env
    body = json.dumps(payload(text_msg("Hi"))).encode()
    assert client.post("/webhook/truechoice", content=body,
                       headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 403
    assert client.post("/webhook/truechoice", content=body).status_code == 403
    assert llm.calls == [] and sent.calls.call_count == 0


def test_duplicate_delivery_is_answered_once(env) -> None:
    client, llm, _, sent = env
    llm.script.append(MockLLM.text("Hello!"))
    post(client, payload(text_msg("Hi")))
    post(client, payload(text_msg("Hi")))
    assert len(llm.calls) == 1 and sent.calls.call_count == 2  # one read receipt + one reply


def test_statuses_and_other_numbers_are_ignored(env) -> None:
    client, llm, _, sent = env
    assert post(client, payload(statuses=[{"id": "wamid.out", "status": "delivered"}])).json() == {"received": 0}
    assert post(client, payload(text_msg("Hi"), phone_id="999")).json() == {"received": 0}
    assert llm.calls == [] and sent.calls.call_count == 0


def test_paused_chat_is_stored_but_not_answered(env, clock) -> None:
    client, llm, repo, sent = env
    repo.set_bot_paused("truechoice", CUSTOMER, True, at=clock.now(), reason="handoff")
    post(client, payload(text_msg("Hello, are you there?")))
    assert llm.calls == [] and sent.calls.call_count == 0
    assert repo.list_messages("truechoice", CUSTOMER)[0].text == "Hello, are you there?"


def test_voice_note_gets_text_only_reply_for_now(env) -> None:
    client, llm, repo, sent = env
    voice = {"from": "919847012345", "id": "wamid.v", "timestamp": "1792219612", "type": "audio",
             "audio": {"id": "media-1", "mime_type": "audio/ogg; codecs=opus", "voice": True}}
    post(client, payload(voice))
    assert llm.calls == []
    assert "text messages" in sent_bodies(sent)[0]["text"]["body"]
    assert [m.source for m in repo.list_messages("truechoice", CUSTOMER)] == ["voice", "text"]


def test_failed_send_does_not_crash(env) -> None:
    client, llm, _, sent = env
    sent.mock(return_value=httpx.Response(400, json={"error": {"message": "Re-engagement message", "code": 131047}}))
    llm.script.append(MockLLM.text("Hello!"))
    assert post(client, payload(text_msg("Hi"))).status_code == 200


# --------------------------------------------------------------------------- unit pieces


def test_parse_button_interactive_and_bad_sender() -> None:
    button = {"from": "919847012345", "id": "b1", "timestamp": "1", "type": "button",
              "button": {"text": "Stop alerts", "payload": "STOP"}, "context": {"id": "wamid.alert"}}
    interactive = {"from": "971501234567", "id": "i1", "timestamp": "1", "type": "interactive",
                   "interactive": {"type": "button_reply", "button_reply": {"id": "x", "title": "Yes"}}}
    junk = {"from": "12", "id": "j1", "timestamp": "1", "type": "text", "text": {"body": "hi"}}
    parsed = parse_webhook(payload(button, interactive, junk), PHONE_ID)
    assert [(m.id, m.phone, m.text, m.is_text) for m in parsed] == [
        ("b1", "+919847012345", "Stop alerts", True), ("i1", "+971501234567", "Yes", True)]
    assert parsed[0].context_id == "wamid.alert" and parsed[0].profile_name == "Arun"


def test_signature_check() -> None:
    body = b'{"a": 1}'
    good = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_signature("s", body, f"sha256={good}")
    assert not verify_signature("s", body + b" ", f"sha256={good}")
    assert not verify_signature("s", body, good) and not verify_signature("s", body, None)


@respx.mock
def test_client_error_carries_graph_code_and_long_text_is_cut() -> None:
    route = respx.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "m"}]}))
    wa = WhatsAppClient(PHONE_ID, "t")
    wa.send_text(CUSTOMER, "x" * 5000)
    assert len(json.loads(route.calls[0].request.content)["text"]["body"]) == 4096
    route.mock(return_value=httpx.Response(400, json={"error": {"message": "outside window", "code": 131047}}))
    with pytest.raises(WhatsAppError) as info:
        wa.send_text(CUSTOMER, "hi")
    assert info.value.code == 131047 and info.value.status == 400
    assert wa.mark_read("wamid.1") is False  # best effort, never raises
