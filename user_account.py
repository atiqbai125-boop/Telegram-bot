from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from config import Settings
from services.downloader import DownloadError, DownloadedTrack, download_song, download_video
from services.formatting import build_media_caption_html, build_userbot_search_html, format_user_error
from services.youtube_search import SongCandidate, get_url_metadata, is_url, search_youtube

logger = logging.getLogger("song_bot.user_account")
COMMAND_RE = re.compile(r"^\s*([!/])\s*(song|video|search)(?:@[A-Za-z0-9_]+)?(?:\s+([\s\S]*))?\s*$", re.IGNORECASE)
PICK_RE = re.compile(r"^\s*([1-5])([aAvV]?)\s*$")


def is_authorized_trigger(
    sender_id: Optional[int],
    owner_id: Optional[int],
    has_reply: bool,
    replied_sender_id: Optional[int],
) -> bool:
    """Owner may trigger anywhere; other users must reply to owner's message."""
    if owner_id is not None and sender_id == owner_id:
        return True
    return bool(has_reply and owner_id is not None and replied_sender_id == owner_id)


class UserAccountController:
    """Own-account Telegram client. OTP/password values remain memory-only."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Optional[TelegramClient] = None
        self.stage = "disconnected"
        self.phone = ""
        self._phone_code_hash = ""
        self._api_id = 0
        self._lock = asyncio.Lock()
        self._downloads: dict[int, asyncio.Lock] = {}
        self._searches: dict[str, dict] = {}
        self._event_handlers_added = False
        self._authorized_user = ""
        self._owner_user_id: Optional[int] = None
        self._last_error = ""

    @property
    def config_path(self) -> Path:
        return self.settings.userbot_config_path

    @property
    def session_path(self) -> Path:
        return self.settings.data_dir / "user_account.session"

    def _save_api_id(self, api_id: int) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"api_id": api_id}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.config_path)
        os.chmod(self.config_path, 0o600)

    def _load_api_id(self) -> Optional[int]:
        try:
            obj = json.loads(self.config_path.read_text(encoding="utf-8"))
            return int(obj["api_id"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _new_client(self, api_id: int, api_hash: str) -> TelegramClient:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Create with owner-only permissions before Telethon opens the SQLite session.
        fd = os.open(self.session_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.session_path, 0o600)
        return TelegramClient(
            str(self.session_path), api_id, api_hash,
            system_lang_code="en", lang_code="en", device_model="Song Dashboard",
            app_version="1.0",
        )

    async def startup(self) -> None:
        api_id = self._load_api_id()
        if not api_id or not self.session_path.exists():
            return
        client = self._new_client(api_id, "stored-session-reconnect")
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self._remove_session_files()
                return
            self.client = client
            self._api_id = api_id
            await self._mark_connected()
            logger.info("Linked Telegram user account restored on startup")
        except Exception:
            logger.exception("Could not restore linked Telegram account")
            try:
                await client.disconnect()
            except Exception:
                pass
            self.stage = "disconnected"

    async def begin_login(self, api_id: int, api_hash: str, phone: str) -> str:
        async with self._lock:
            if self.stage == "connected":
                raise ValueError("Disconnect the currently linked account before linking another one.")
            if self.stage in {"awaiting_code", "awaiting_password"} and self.client:
                await self.client.disconnect()
                self._remove_session_files()
            self.client = None
            self._event_handlers_added = False
            self._last_error = ""
            self.stage = "connecting"
            self.phone = phone.strip()
            self._api_id = api_id
            client = self._new_client(api_id, api_hash.strip())
            try:
                await client.connect()
                sent = await client.send_code_request(self.phone)
                self._phone_code_hash = sent.phone_code_hash
                self.client = client
                self.stage = "awaiting_code"
                os.chmod(self.session_path, 0o600)
                return "Telegram sent a login code. Enter the code in the dashboard."
            except Exception as exc:
                self._last_error = format_user_error(exc)
                self.stage = "disconnected"
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise ValueError(self._last_error) from None

    async def submit_code(self, code: str) -> str:
        async with self._lock:
            if self.stage != "awaiting_code" or not self.client:
                raise ValueError("Start account linking first, then enter the new code.")
            try:
                await self.client.sign_in(
                    phone=self.phone, code=code.strip(), phone_code_hash=self._phone_code_hash
                )
            except SessionPasswordNeededError:
                self.stage = "awaiting_password"
                return "This account has two-step verification enabled. Enter its password."
            except Exception as exc:
                raise ValueError(format_user_error(exc)) from None
            await self._finish_login()
            return "Telegram account linked successfully."

    async def submit_password(self, password: str) -> str:
        async with self._lock:
            if self.stage != "awaiting_password" or not self.client:
                raise ValueError("Two-step verification is not currently requested.")
            try:
                await self.client.sign_in(password=password)
            except Exception as exc:
                raise ValueError(format_user_error(exc)) from None
            await self._finish_login()
            return "Telegram account linked successfully."

    async def _finish_login(self) -> None:
        assert self.client is not None
        me = await self.client.get_me()
        self._authorized_user = ("@" + me.username) if me.username else me.first_name or "Telegram account"
        self._owner_user_id = me.id
        self._save_api_id(self._api_id)
        os.chmod(self.session_path, 0o600)
        self.phone = ""
        self._phone_code_hash = ""
        self.stage = "connected"
        await self._install_handlers()
        logger.info("Telegram user account linked; user_id=%s", me.id)

    async def _mark_connected(self) -> None:
        assert self.client is not None
        me = await self.client.get_me()
        self._authorized_user = ("@" + me.username) if me.username else me.first_name or "Telegram account"
        self._owner_user_id = me.id
        self.stage = "connected"
        await self._install_handlers()

    async def _install_handlers(self) -> None:
        assert self.client is not None
        if self._event_handlers_added:
            return
        # Listen to the owner's outgoing messages too, for self-reply downloads.
        # The handler itself ignores every non-command and validates reply context.
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self._event_handlers_added = True

    async def disconnect(self) -> str:
        async with self._lock:
            client = self.client
            self.client = None
            self.stage = "disconnected"
            self._authorized_user = ""
            self._owner_user_id = None
            self._event_handlers_added = False
            self.phone = ""
            self._phone_code_hash = ""
            if client:
                try:
                    if await client.is_user_authorized():
                        await client.log_out()
                    else:
                        await client.disconnect()
                except Exception:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            self._remove_session_files()
            try:
                self.config_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._searches.clear()
            return "This dashboard session was disconnected. Other Telegram sessions are unchanged."

    def _remove_session_files(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                Path(str(self.session_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    def status(self) -> dict[str, str | bool]:
        return {
            "connected": self.stage == "connected",
            "stage": self.stage,
            "account": self._authorized_user,
            "error": self._last_error,
        }

    def _store_search(self, candidates: list[SongCandidate], sender_id: int, chat_id: int, status_id: int, origin_id: int) -> str:
        if len(self._searches) > 80:
            self._searches.pop(next(iter(self._searches)))
        token = uuid.uuid4().hex[:8]
        self._searches[token] = {
            "candidates": candidates, "sender_id": sender_id, "chat_id": chat_id,
            "status_id": status_id, "origin_id": origin_id,
            "created": time.monotonic(),
        }
        return token

    async def _on_new_message(self, event) -> None:
        if self.stage != "connected" or not self.client or self._owner_user_id is None:
            return
        text = (event.raw_text or "").strip()
        match = COMMAND_RE.match(text)
        selection = PICK_RE.match(text)
        if not match and not selection:
            # Ordinary text and /start are always ignored.
            return

        reply_id = getattr(event.message, "reply_to_msg_id", None)
        replied_sender_id = None
        is_owner = event.sender_id == self._owner_user_id
        if not is_owner:
            # Other people can trigger work only by replying to the linked account.
            if not reply_id:
                return
            try:
                replied = await event.get_reply_message()
                replied_sender_id = getattr(replied, "sender_id", None) if replied else None
            except Exception:
                return
        if not is_authorized_trigger(
            event.sender_id, self._owner_user_id, bool(reply_id), replied_sender_id
        ):
            return

        chat_id = event.chat_id
        lock = self._downloads.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            if match:
                command = match.group(2).lower()
                query = (match.group(3) or "").strip()
                if command not in {"song", "video", "search"} or not query:
                    return
                await self._process_command(event, query, command)
            else:
                await self._process_selection(event, selection)

    async def _process_selection(self, event, selection) -> None:
        reply_to_id = getattr(event.message, "reply_to_msg_id", None)
        if not reply_to_id:
            return
        selected_index = int(selection.group(1)) - 1
        suffix = selection.group(2).lower()
        mode = "video" if suffix == "v" else "audio"
        sender_id = event.sender_id
        saved = next((
            item for item in reversed(list(self._searches.values()))
            if item["chat_id"] == event.chat_id
            and item["sender_id"] == sender_id
            and item["status_id"] == reply_to_id
        ), None)
        if not saved:
            # A bare number is ordinary text unless it replies to this user's result card.
            return
        if time.monotonic() - saved["created"] > 900:
            await self.client.send_message(
                event.chat_id, "Those search results have expired. Please run <code>/search &lt;name&gt;</code> again.",
                parse_mode="html", reply_to=event.message.id,
            )
            return
        candidates = saved["candidates"]
        if selected_index >= len(candidates):
            await self.client.send_message(
                event.chat_id, "Choose a result number shown in the search list.",
                reply_to=event.message.id,
            )
            return
        status_msg = await self.client.send_message(
            event.chat_id, "⠋ <i>Preparing your download...</i>", parse_mode="html",
            reply_to=saved["origin_id"],
        )
        try:
            sender = await event.get_sender()
            await self._download_and_send(
                event.chat_id, saved["origin_id"], sender,
                [candidates[selected_index]], mode, status_msg,
            )
        finally:
            # Each search selection is one-time to prevent accidental duplicate uploads.
            for key, value in list(self._searches.items()):
                if value is saved:
                    self._searches.pop(key, None)
                    break

    async def _process_command(self, event, query: str, command: str) -> None:
        assert self.client is not None
        mode = "video" if command == "video" else "audio"
        header = "Searching YouTube" if command == "search" else ("Downloading Video" if mode == "video" else "Downloading Audio")
        status_text = (
            f"{'🔍' if command == 'search' else ('🎬' if mode == 'video' else '🎧')} <b>{header}</b>\n\n"
            f"<blockquote>Query: <code>{html.escape(query[:100])}</code></blockquote>\n"
            "⠋ <i>Searching...</i>\n<code>▰▱▱▱▱▱▱▱▱▱</code>  <b>10%</b>"
        )
        status_msg = None
        try:
            status_msg = await self.client.send_message(
                event.chat_id, status_text, parse_mode="html", reply_to=event.message.id,
                link_preview=False,
            )
            if is_url(query):
                candidates = [await get_url_metadata(query)]
            else:
                candidates = await search_youtube(query, limit=5)
            if not candidates:
                await self._edit(status_msg, "❌ <b>No results found.</b>\n<blockquote>Try another title or a YouTube link.</blockquote>")
                return
            if command == "search":
                self._store_search(candidates, event.sender_id, event.chat_id, status_msg.id, event.message.id)
                await self._edit(status_msg, build_userbot_search_html(candidates))
                return
            await self._download_and_send(
                event.chat_id, event.message.id, event.sender, candidates, mode, status_msg
            )
        except Exception as exc:
            logger.warning("Linked account command failed (%s): %s", command, type(exc).__name__)
            if status_msg:
                try:
                    await self._edit(status_msg, f"❌ <b>Request failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>")
                except Exception:
                    pass

    async def _edit(self, message, text: str, buttons=None) -> None:
        try:
            await message.edit(text, parse_mode="html", buttons=buttons, link_preview=False)
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("User account message edit skipped: %s", type(exc).__name__)

    async def _download_and_send(self, chat_id: int, reply_to: int, sender, candidates: list[SongCandidate], mode: str, status_msg) -> None:
        assert self.client is not None
        last_text = [""]
        last_edit_at = [0.0]
        async def update_status(text: str) -> None:
            now = time.monotonic()
            if text == last_text[0] or now - last_edit_at[0] < 1.25:
                return
            await self._edit(status_msg, text)
            last_text[0] = text
            last_edit_at[0] = now

        try:
            with tempfile.TemporaryDirectory(prefix="tg_user_media_") as tmp:
                work = Path(tmp)
                if mode == "video":
                    track = await download_video(
                        candidates, work, self.settings.api_base_url,
                        self.settings.max_telegram_bytes, quality="hd", status_callback=update_status,
                    )
                else:
                    track = await download_song(
                        candidates, work, self.settings.api_base_url,
                        self.settings.max_telegram_bytes, status_callback=update_status,
                    )
                caption = build_media_caption_html(
                    track, requester_id=getattr(sender, "id", None),
                    requester_name=(getattr(sender, "first_name", None) or getattr(sender, "username", None) or "User"),
                )
                attrs = None
                if track.media_type == "audio":
                    attrs = [DocumentAttributeAudio(
                        duration=int(track.duration_seconds or 0),
                        title=track.title[:128], performer=track.performer[:128],
                    )]
                elif track.width and track.height:
                    attrs = [DocumentAttributeVideo(
                        duration=int(track.duration_seconds or 0), w=track.width, h=track.height,
                        supports_streaming=True,
                    )]
                await self.client.send_file(
                    chat_id, str(track.file_path), caption=caption, parse_mode="html",
                    reply_to=reply_to, file_size=track.size_bytes, attributes=attrs,
                    thumb=str(track.thumbnail_path) if track.thumbnail_path and track.thumbnail_path.exists() else None,
                    supports_streaming=track.media_type == "video",
                )
            try:
                await status_msg.delete()
            except Exception:
                await self._edit(status_msg, "✅ <b>Download complete.</b>")
        except Exception as exc:
            logger.warning("Linked account upload failed: %s", type(exc).__name__)
            await self._edit(status_msg, f"❌ <b>Download failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>")
