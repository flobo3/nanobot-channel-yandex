"""Yandex Messenger (Ya.Messenger) channel for nanobot.

Inbound:
- Polling mode: periodic POST to /messages/getUpdates/
- Webhook mode: HTTP server receives POST with {updates: [...]}
- Parse text messages, identify sender (login) and chat (id + type)
- Publish to nanobot bus via BaseChannel._handle_message()

Outbound:
- Send text via POST /messages/sendText/
- Send files via POST /messages/sendFile/
- Send images via POST /messages/sendImage/
- Delete messages via POST /messages/delete/

Limitations:
- No edit message API -> streaming sends complete messages (no delta)
- Inline keyboard buttons supported but only via send (not update)
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base
from pydantic import Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE = "https://botapi.messenger.yandex.net/bot/v1"
_SEND_TEXT = f"{_API_BASE}/messages/sendText/"
_SEND_FILE = f"{_API_BASE}/messages/sendFile/"
_SEND_IMAGE = f"{_API_BASE}/messages/sendImage/"
_DELETE_MSG = f"{_API_BASE}/messages/delete/"
_GET_UPDATES = f"{_API_BASE}/messages/getUpdates/"

_MAX_SEEN = 500
_DEFAULT_POLL_INTERVAL = 2
_LONG_POLL_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class YandexConfig(Base):
    """Yandex Messenger channel configuration."""

    enabled: bool = False
    token: str = ""
    mode: str = "polling"  # "polling" or "webhook"
    webhook_url: str = ""
    poll_interval: int = _DEFAULT_POLL_INTERVAL
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = False


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class YandexChannel(BaseChannel):
    """Yandex Messenger channel using Bot API (polling or webhook)."""

    name = "yandex"
    display_name = "Yandex Messenger"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return YandexConfig().model_dump()

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = YandexConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: YandexConfig = config
        self._client: httpx.AsyncClient | None = None
        self._seen_updates: deque[int] = deque(maxlen=_MAX_SEEN)
        self._last_update_id: int = 0
        self._poll_task: asyncio.Task | None = None
        self._webhook_server: asyncio.Task | None = None

    # -- HTTP helpers --

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"OAuth {self.config.token}",
            "Content-Type": "application/json",
        }

    async def _api_post(self, url: str, payload: dict) -> dict:
        """Send JSON POST to Yandex Bot API. Returns parsed response."""
        assert self._client is not None
        resp = await self._client.post(url, json=payload, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _api_upload(self, url: str, file_path: str, chat_id: str | None = None, login: str | None = None) -> dict:
        """Upload a file (multipart/form-data) to Yandex Bot API."""
        assert self._client is not None
        path = Path(file_path)
        if not path.exists():
            logger.error("Yandex: file not found: {}", file_path)
            return {"ok": False}

        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        # Build multipart fields
        fields: dict[str, Any] = {}
        if chat_id:
            fields["chat_id"] = chat_id
        if login:
            fields["login"] = login

        with open(path, "rb") as f:
            files = {"file": (path.name, f, mime)}
            headers = {"Authorization": f"OAuth {self.config.token}"}
            resp = await self._client.post(
                url, data=fields, files=files, headers=headers, timeout=60,
            )
        resp.raise_for_status()
        return resp.json()

    # -- Lifecycle --

    async def start(self) -> None:
        if not self.config.token:
            logger.error("Yandex: token not configured")
            return

        self._client = httpx.AsyncClient(timeout=_LONG_POLL_TIMEOUT)
        self._running = True

        if self.config.mode == "webhook" and self.config.webhook_url:
            self._start_webhook()
        else:
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("Yandex: polling started (interval={}s)", self.config.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._webhook_server:
            self._webhook_server.cancel()
            self._webhook_server = None
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- Polling --

    async def _poll_loop(self) -> None:
        """Continuously poll for updates using offset-based pagination."""
        # Skip historical messages on first start
        await self._skip_history()

        while self._running:
            try:
                updates = await self._fetch_updates()
                if updates:
                    for update in updates:
                        await self._process_update(update)
                    # Advance cursor past the last seen update
                    self._last_update_id = max(u.get("update_id", 0) for u in updates)
            except httpx.HTTPStatusError as e:
                logger.warning("Yandex: API error during polling: {}", e)
            except Exception as e:
                logger.warning("Yandex: polling error: {}", e)

            await asyncio.sleep(self.config.poll_interval)

    async def _skip_history(self) -> None:
        """Fetch and discard all pending updates to start from current."""
        try:
            updates = await self._fetch_updates()
            if updates:
                self._last_update_id = max(u.get("update_id", 0) for u in updates)
                logger.debug("Yandex: skipped {} historical updates", len(updates))
        except Exception as e:
            logger.debug("Yandex: skip history failed: {}", e)

    async def _fetch_updates(self) -> list[dict]:
        """Fetch updates from getUpdates endpoint."""
        payload = {
            "limit": 100,
            "offset": self._last_update_id + 1,
        }
        data = await self._api_post(_GET_UPDATES, payload)
        return data.get("updates", [])

    # -- Webhook --

    def _start_webhook(self) -> None:
        """Start a lightweight HTTP server for webhook mode."""
        # Webhook URL is configured in Yandex 360 admin panel.
        # We run a simple HTTP listener here. Requires aiohttp or similar.
        # For now, log a warning — webhook mode needs a reverse proxy setup.
        logger.warning(
            "Yandex: webhook mode requires an external HTTP server "
            "(e.g. via nanobot gateway) to forward POST to _process_update(). "
            "Falling back to polling."
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def handle_webhook(self, body: dict) -> None:
        """Process a webhook payload. Call this from your HTTP handler.

        Expected body format:
        {
            "updates": [
                {
                    "update_id": 123,
                    "message_id": 456,
                    "text": "hello",
                    "from": {"login": "user@org.ru", "id": "...", "display_name": "..."},
                    "chat": {"id": "...", "type": "private|group"},
                    "timestamp": 1720696221
                }
            ]
        }
        """
        for update in body.get("updates", []):
            await self._process_update(update)

    # -- Inbound processing --

    async def _process_update(self, update: dict) -> None:
        """Parse a single update and publish to nanobot bus."""
        update_id = update.get("update_id", 0)

        # Deduplicate
        if update_id in self._seen_updates:
            return
        self._seen_updates.append(update_id)

        text = update.get("text", "")
        if not text:
            return

        from_user = update.get("from", {})
        chat = update.get("chat", {})

        sender_id = from_user.get("login", from_user.get("id", "unknown"))
        chat_id = chat.get("id", "")
        chat_type = chat.get("type", "private")

        # For private chats, Yandex doesn't always provide chat_id.
        # Use login as chat_id for DM sessions.
        if not chat_id and chat_type == "private":
            chat_id = f"dm:{sender_id}"

        metadata = {
            "chat_type": chat_type,
            "message_id": str(update.get("message_id", "")),
            "from_display_name": from_user.get("display_name", ""),
            "from_id": from_user.get("id", ""),
            "is_bot": from_user.get("robot", False),
        }

        # Thread support
        thread_id = update.get("thread_id")
        if thread_id:
            metadata["thread_id"] = str(thread_id)

        # Button callback — treat as regular text
        callback_data = update.get("callback_data")
        if callback_data:
            metadata["callback_data"] = callback_data

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )

    # -- Outbound --

    async def send(self, msg: OutboundMessage) -> None:
        """Send an outbound message to Yandex Messenger."""
        chat_id = msg.chat_id
        text = msg.content

        if not text and not msg.media:
            return

        # Resolve recipient: chat_id may be "dm:user@org.ru" from private chats
        payload: dict[str, Any] = {"text": text}
        if chat_id.startswith("dm:"):
            payload["login"] = chat_id[3:]
        else:
            payload["chat_id"] = chat_id

        # Thread support
        thread_id = msg.metadata.get("thread_id") if msg.metadata else None
        if thread_id:
            payload["thread_id"] = thread_id

        # Send media first
        if msg.media:
            for media_path in msg.media:
                await self._send_media(media_path, payload)

        # Send text
        if text:
            try:
                result = await self._api_post(_SEND_TEXT, payload)
                if result.get("ok"):
                    logger.debug("Yandex: sent message_id={}", result.get("message_id"))
                else:
                    logger.warning("Yandex: send failed: {}", result)
            except httpx.HTTPStatusError as e:
                logger.error("Yandex: send error: {} {}", e.response.status_code, e.response.text)
                raise

    async def _send_media(self, media_path: str, recipient: dict[str, Any]) -> None:
        """Send a file or image, auto-detecting type."""
        ext = Path(media_path).suffix.lower()
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

        chat_id = recipient.get("chat_id")
        login = recipient.get("login")

        if ext in image_exts:
            url = _SEND_IMAGE
        else:
            url = _SEND_FILE

        try:
            result = await self._api_upload(url, media_path, chat_id=chat_id, login=login)
            if result.get("ok"):
                logger.debug("Yandex: sent media {}", media_path)
            else:
                logger.warning("Yandex: media send failed: {}", result)
        except Exception as e:
            logger.error("Yandex: media upload error: {}", e)

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Streaming not supported — accumulate and send on flush."""
        # Yandex API has no edit message endpoint.
        # Accumulate deltas in a buffer; the channel manager calls send() on flush.
        pass

    async def delete_message(self, message_id: int | str, chat_id: str | None = None, login: str | None = None) -> dict:
        """Delete a message by message_id."""
        payload: dict[str, Any] = {"message_id": int(message_id)}
        if chat_id:
            payload["chat_id"] = chat_id
        if login:
            payload["login"] = login
        return await self._api_post(_DELETE_MSG, payload)
