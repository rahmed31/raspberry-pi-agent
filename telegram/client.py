"""
telegram/client.py

Async Telegram Bot API client using aiohttp.
Replaces the synchronous requests-based client.

Supports:
- Long polling for updates
- Sending text (with automatic chunking for long messages)
- Sending files: photos, videos, audio, documents
- Downloading received files
- Extracting file info from incoming updates
"""

import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from agent.config import MAX_TELEGRAM_FILE_BYTES

LOGGER = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    pass


class FileTooLargeError(ValueError):
    """Raised when a file exceeds Telegram's 50MB send limit."""
    pass


def _check_file_size(file_path: str) -> None:
    try:
        size = Path(file_path).stat().st_size
        if size > MAX_TELEGRAM_FILE_BYTES:
            size_mb = size / (1024 * 1024)
            raise FileTooLargeError(
                f"File `{Path(file_path).name}` is {size_mb:.1f}MB — "
                f"exceeds Telegram's 50MB limit. "
                f"Retrieve it directly from the server at: {file_path}"
            )
    except OSError as exc:
        raise FileTooLargeError(f"Cannot read file {file_path}: {exc}") from exc


class TelegramClient:
    """
    Async Telegram Bot API client.

    Usage:
        client = TelegramClient(token, default_chat_id=chat_id)
        async with client:
            await client.send_message("hello")
    """

    def __init__(
        self,
        bot_token: str,
        default_chat_id: Optional[int] = None,
        request_timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")

        self.bot_token      = bot_token
        self.default_chat_id = default_chat_id
        self.request_timeout = request_timeout
        self.max_retries    = max_retries
        self.retry_backoff  = retry_backoff
        self.base_url       = f"https://api.telegram.org/bot{bot_token}"
        self.file_url       = f"https://api.telegram.org/file/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "TelegramClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def open(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout + 15)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("TelegramClient session not open — use async with or call open()")
        return self._session

    # ------------------------------------------------------------------ #
    #  Core request                                                        #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[aiohttp.FormData] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{method}"
        request_timeout = aiohttp.ClientTimeout(total=timeout or self.request_timeout + 15)

        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if data is not None:
                    resp = await self.session.post(url, data=data, timeout=request_timeout)
                elif params is not None:
                    resp = await self.session.get(url, params=params, timeout=request_timeout)
                else:
                    resp = await self.session.get(url, timeout=request_timeout)

                async with resp:
                    payload = await resp.json()

                if not payload.get("ok", False):
                    raise TelegramAPIError(f"Telegram API error for {method}: {payload}")

                return payload

            except (aiohttp.ClientError, TelegramAPIError) as exc:
                last_exc = exc
                LOGGER.warning(
                    "Telegram request failed (attempt %d/%d): %s %s",
                    attempt, self.max_retries, method, exc,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff * attempt)

        raise TelegramAPIError(
            f"Telegram request failed after {self.max_retries} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------ #
    #  Webhook / polling                                                   #
    # ------------------------------------------------------------------ #

    async def delete_webhook(self, drop_pending_updates: bool = False) -> Dict[str, Any]:
        data = aiohttp.FormData()
        data.add_field("drop_pending_updates", str(drop_pending_updates).lower())
        return await self._request("deleteWebhook", data=data)

    async def get_me(self) -> Dict[str, Any]:
        return await self._request("getMe")

    async def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 25,
        allowed_updates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = str(allowed_updates).replace("'", '"')

        payload = await self._request(
            "getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        return payload.get("result", [])

    # ------------------------------------------------------------------ #
    #  Send text                                                           #
    # ------------------------------------------------------------------ #

    async def send_message(
        self,
        text: str,
        chat_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        target_chat_id = chat_id if chat_id is not None else self.default_chat_id
        if target_chat_id is None:
            raise ValueError("chat_id is required")

        data = aiohttp.FormData()
        data.add_field("chat_id", str(target_chat_id))
        data.add_field("text", text)
        data.add_field("disable_notification", str(disable_notification).lower())
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
        if parse_mode:
            data.add_field("parse_mode", parse_mode)

        return await self._request("sendMessage", data=data)

    async def send_long_message(
        self,
        text: str,
        chat_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False,
        chunk_size: int = 3500,
    ) -> List[Dict[str, Any]]:
        chunks  = self._split_text(text, chunk_size=chunk_size)
        results = []
        for index, chunk in enumerate(chunks):
            results.append(
                await self.send_message(
                    text=chunk,
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                )
            )
        return results

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        data.add_field("action", action)
        try:
            await self._request("sendChatAction", data=data)
        except Exception:
            pass  # typing indicators are non-critical

    # ------------------------------------------------------------------ #
    #  Send files                                                          #
    # ------------------------------------------------------------------ #

    async def send_photo(
        self,
        file_path: str,
        chat_id: Optional[int] = None,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_file_size(file_path)
        target_chat_id = chat_id if chat_id is not None else self.default_chat_id
        if target_chat_id is None:
            raise ValueError("chat_id is required")

        data = aiohttp.FormData()
        data.add_field("chat_id", str(target_chat_id))
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
        with open(file_path, "rb") as f:
            data.add_field("photo", f, filename=Path(file_path).name)
            return await self._request("sendPhoto", data=data, timeout=120)

    async def send_video(
        self,
        file_path: str,
        chat_id: Optional[int] = None,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_file_size(file_path)
        target_chat_id = chat_id if chat_id is not None else self.default_chat_id
        if target_chat_id is None:
            raise ValueError("chat_id is required")

        data = aiohttp.FormData()
        data.add_field("chat_id", str(target_chat_id))
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
        with open(file_path, "rb") as f:
            data.add_field("video", f, filename=Path(file_path).name)
            return await self._request("sendVideo", data=data, timeout=120)

    async def send_audio(
        self,
        file_path: str,
        chat_id: Optional[int] = None,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_file_size(file_path)
        target_chat_id = chat_id if chat_id is not None else self.default_chat_id
        if target_chat_id is None:
            raise ValueError("chat_id is required")

        data = aiohttp.FormData()
        data.add_field("chat_id", str(target_chat_id))
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
        with open(file_path, "rb") as f:
            data.add_field("audio", f, filename=Path(file_path).name)
            return await self._request("sendAudio", data=data, timeout=120)

    async def send_document(
        self,
        file_path: str,
        chat_id: Optional[int] = None,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_file_size(file_path)
        target_chat_id = chat_id if chat_id is not None else self.default_chat_id
        if target_chat_id is None:
            raise ValueError("chat_id is required")

        data = aiohttp.FormData()
        data.add_field("chat_id", str(target_chat_id))
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
        with open(file_path, "rb") as f:
            data.add_field("document", f, filename=Path(file_path).name)
            return await self._request("sendDocument", data=data, timeout=120)

    async def send_file(
        self,
        file_path: str,
        chat_id: Optional[int] = None,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Smart send — picks the right method based on file extension."""
        ext = Path(file_path).suffix.lower()

        if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return await self.send_photo(file_path, chat_id=chat_id, caption=caption,
                                         reply_to_message_id=reply_to_message_id)
        elif ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            return await self.send_video(file_path, chat_id=chat_id, caption=caption,
                                         reply_to_message_id=reply_to_message_id)
        elif ext in {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"}:
            return await self.send_audio(file_path, chat_id=chat_id, caption=caption,
                                         reply_to_message_id=reply_to_message_id)
        else:
            return await self.send_document(file_path, chat_id=chat_id, caption=caption,
                                            reply_to_message_id=reply_to_message_id)

    # ------------------------------------------------------------------ #
    #  Receive files                                                       #
    # ------------------------------------------------------------------ #

    async def get_file_info(self, file_id: str) -> Dict[str, Any]:
        return await self._request("getFile", params={"file_id": file_id})

    async def download_file(self, file_id: str, destination: str) -> str:
        info      = await self.get_file_info(file_id)
        file_path = info["result"]["file_path"]
        url       = f"{self.file_url}/{file_path}"

        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)

        timeout = aiohttp.ClientTimeout(total=120)
        async with self.session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)

        return str(dest)

    # ------------------------------------------------------------------ #
    #  Incoming file extraction                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_incoming_file(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        message = update.get("message", {})

        if "photo" in message:
            photo = message["photo"][-1]
            return {
                "file_id":   photo["file_id"],
                "file_name": f"photo_{photo['file_id'][:8]}.jpg",
                "file_type": "photo",
                "mime_type": "image/jpeg",
                "caption":   message.get("caption"),
            }

        if "video" in message:
            video = message["video"]
            return {
                "file_id":   video["file_id"],
                "file_name": video.get("file_name", f"video_{video['file_id'][:8]}.mp4"),
                "file_type": "video",
                "mime_type": video.get("mime_type", "video/mp4"),
                "caption":   message.get("caption"),
            }

        if "audio" in message:
            audio = message["audio"]
            return {
                "file_id":   audio["file_id"],
                "file_name": audio.get("file_name", f"audio_{audio['file_id'][:8]}.mp3"),
                "file_type": "audio",
                "mime_type": audio.get("mime_type", "audio/mpeg"),
                "caption":   message.get("caption"),
            }

        if "voice" in message:
            voice = message["voice"]
            return {
                "file_id":   voice["file_id"],
                "file_name": f"voice_{voice['file_id'][:8]}.ogg",
                "file_type": "voice",
                "mime_type": "audio/ogg",
                "caption":   None,
            }

        if "document" in message:
            doc = message["document"]
            return {
                "file_id":   doc["file_id"],
                "file_name": doc.get("file_name", f"document_{doc['file_id'][:8]}"),
                "file_type": "document",
                "mime_type": doc.get("mime_type"),
                "caption":   message.get("caption"),
            }

        return None

    # ------------------------------------------------------------------ #
    #  Update extraction helpers                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_chat_id(update: Dict[str, Any]) -> Optional[int]:
        return update.get("message", {}).get("chat", {}).get("id")

    @staticmethod
    def extract_message_id(update: Dict[str, Any]) -> Optional[int]:
        return update.get("message", {}).get("message_id")

    @staticmethod
    def extract_text(update: Dict[str, Any]) -> Optional[str]:
        return update.get("message", {}).get("text")

    @staticmethod
    def extract_username(update: Dict[str, Any]) -> Optional[str]:
        sender = update.get("message", {}).get("from", {})
        return sender.get("username") or sender.get("first_name")

    @staticmethod
    def extract_update_id(update: Dict[str, Any]) -> int:
        return update["update_id"]

    # ------------------------------------------------------------------ #
    #  Text splitting                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_text(text: str, chunk_size: int = 3500) -> List[str]:
        if len(text) <= chunk_size:
            return [text]

        chunks: List[str] = []
        remaining = text

        while len(remaining) > chunk_size:
            split_at = remaining.rfind("\n", 0, chunk_size)
            if split_at == -1:
                split_at = remaining.rfind(" ", 0, chunk_size)
            if split_at == -1:
                split_at = chunk_size
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()

        if remaining:
            chunks.append(remaining)

        return chunks
