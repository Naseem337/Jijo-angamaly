from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

import app.conversation.photos as photos_module
from app.agent.tools import ToolContext, send_car_photo
from app.conversation.photos import PhotoSender, caption
from app.core.client_config import load_client_config
from app.inventory import Car, MockInventoryAdapter
from app.providers.audio import find_ffmpeg
from app.providers.images import to_jpeg
from app.storage.sqlite_repo import SQLiteRepository
from app.whatsapp.client import WhatsAppClient

PHOTO = "https://carlet-used-cars.s3.ap-south-1.amazonaws.com/cars/original/123-WhatsApp Image 1.webp"
PHONE = "+919847012345"


def car(car_id: str, image: str | None = PHOTO) -> Car:
    return Car(id=car_id, make="Maruti Suzuki", model="Ciaz Zeta", year=2018, price=595000, km=38000,
               fuel="petrol", transmission="automatic", location="Makkaraparamba showroom", image_url=image)


STOCK = MockInventoryAdapter([car("TC-A72F0B"), car("TC-000002"), car("TC-000003"), car("TC-000004"),
                              car("TC-NOPHOT", image=None)])


def ctx(clients_dir: Path) -> ToolContext:
    return ToolContext(load_client_config("truechoice", clients_dir), None, STOCK, PHONE,
                       datetime(2026, 10, 20, tzinfo=timezone.utc))


def test_photo_tool_queues_main_photo_max_three(clients_dir) -> None:
    c = ctx(clients_dir)
    result = json.loads(send_car_photo(c, {"car_ids": ["tc-a72f0b", "TC-A72F0B", "TC-000002", "TC-000003",
                                                       "TC-000004", "TC-NOPHOT", "TC-MISSING"]}).content)
    assert c.photos == ["TC-A72F0B", "TC-000002", "TC-000003"]  # deduped, at most 3
    assert result["no_photo_for"] == ["TC-NOPHOT", "TC-MISSING"] and "team sends them" in result["note"]


def test_caption() -> None:
    assert caption(car("TC-A72F0B")) == "*2018 Maruti Suzuki Ciaz Zeta* · ₹5.95 lakh · 38,000 km · " \
                                        "Makkaraparamba showroom (TC-A72F0B)"


@respx.mock
def test_photo_is_converted_uploaded_sent_and_cached(clients_dir, tmp_path, monkeypatch, clock) -> None:
    monkeypatch.setattr(photos_module, "to_jpeg", lambda data, ffmpeg: b"\xff\xd8\xff" + data)
    download = respx.get(PHOTO).mock(return_value=httpx.Response(200, content=b"RIFFwebp"))
    upload = respx.post("https://graph.facebook.com/v25.0/123/media").mock(
        return_value=httpx.Response(200, json={"id": "img-1"}))
    send = respx.post("https://graph.facebook.com/v25.0/123/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.p"}]}))
    repo = SQLiteRepository(tmp_path / "p.db")
    sender = PhotoSender(load_client_config("truechoice", clients_dir), lambda: STOCK, WhatsAppClient("123", "t"),
                         repo, clock, "ffmpeg")
    assert sender.send(PHONE, "TC-A72F0B") and sender.send(PHONE, "TC-A72F0B")
    assert download.call_count == 1 and upload.call_count == 1  # second time: cached media id
    body = json.loads(send.calls[0].request.content)
    assert body["type"] == "image" and body["image"]["id"] == "img-1"
    assert body["image"]["caption"].startswith("*2018 Maruti Suzuki Ciaz Zeta*")
    assert b"image/jpeg" in upload.calls[0].request.content
    assert repo.list_messages("truechoice", PHONE)[-1].meta == {"photo": "TC-A72F0B"}
    assert not sender.send(PHONE, "TC-NOPHOT")
    repo.close()


@pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg not installed")
def test_real_webp_to_jpeg(tmp_path) -> None:
    webp = tmp_path / "car.webp"
    subprocess.run([find_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=2000x1500",
                    "-frames:v", "1", str(webp)], check=True)
    jpeg = to_jpeg(webp.read_bytes(), find_ffmpeg())
    assert jpeg[:3] == b"\xff\xd8\xff" and len(jpeg) < 300_000
