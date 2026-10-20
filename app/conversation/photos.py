"""Sending a car's main photo when the customer asks for it.

Cost control (user decision, 2026-10-20): only the FIRST photo of each car, at most 3 cars per
message; more photos or a video come from the sales team. The agent's `send_car_photo` tool
only picks the cars; the download, JPEG conversion, upload and send happen here, in code.
A photo is uploaded once and its WhatsApp media id reused for 25 days.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import timedelta

import httpx

from app.core.client_config import ClientConfig
from app.core.clock import Clock
from app.core.money import format_lakh
from app.core.phone import mask_phone
from app.inventory import Car, InventoryAdapter, InventoryError
from app.providers.images import ImageError, to_jpeg
from app.storage.models import Message
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

CACHE_DAYS = 25
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


def caption(car: Car) -> str:
    parts = [f"*{car.title}*", format_lakh(car.price), f"{car.km:,} km"]
    if car.location:
        parts.append(car.location)
    return " · ".join(parts) + f" ({car.id})"


class PhotoSender:
    def __init__(self, config: ClientConfig, inventory: Callable[[], InventoryAdapter], whatsapp: WhatsAppClient,
                 repo: Repository, clock: Clock, ffmpeg: str, http: httpx.Client | None = None) -> None:
        self.config = config
        self._inventory = inventory
        self.whatsapp = whatsapp
        self.repo = repo
        self.clock = clock
        self.ffmpeg = ffmpeg
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)

    def send(self, phone: str, car_id: str) -> bool:
        """Send the car's main photo with a one-line caption. False (logged) on any failure."""
        try:
            car = self._inventory().get_car(car_id)
        except InventoryError as exc:
            log.error("Photo for %s: stock unavailable: %s", car_id, exc)
            return False
        if car is None or not car.image_url:
            log.warning("No photo for car %s", car_id)
            return False
        cid, now = self.config.client_id, self.clock.now()
        key = "photo:" + hashlib.sha256(car.image_url.encode()).hexdigest()
        try:
            media_id = self.repo.get_cached_media(cid, key, now)
            if media_id is None:
                media_id = self.whatsapp.upload_media(self._jpeg(car.image_url), "image/jpeg", "car.jpg")
                self.repo.put_cached_media(cid, key, media_id, now + timedelta(days=CACHE_DAYS))
            self.whatsapp.send_image(phone, media_id, caption(car))
        except (ImageError, WhatsAppError, httpx.HTTPError) as exc:
            log.error("Photo of %s to %s failed: %s", car_id, mask_phone(phone), exc)
            return False
        self.repo.add_message(Message(cid, phone, "out", f"[photo] {caption(car)}", self.clock.now(),
                                      source="system", meta={"photo": car.id}))
        return True

    def _jpeg(self, url: str) -> bytes:
        resp = self._http.get(url)
        if resp.status_code != 200:
            raise ImageError(f"photo download HTTP {resp.status_code}")
        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            raise ImageError(f"photo too large ({len(resp.content)} bytes)")
        return to_jpeg(resp.content, self.ffmpeg)
