from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

import httpx

from config import Settings, get_settings
from services.downloader import (
    DownloadError,
    DownloadedTrack,
    download_song,
    download_video,
)
from services.youtube_search import (
    SongCandidate,
    extract_youtube_id,
    get_url_metadata,
    is_url,
    search_youtube,
)

logger = logging.getLogger("song_bot")

_BANG_CMD_RE = re.compile(
    r"^[!/](?:song|video|search)(?:@[A-Za-z0-9_]+)?(?:\s+([\s\S]+))?$",
    re.IGNORECASE,
)


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                Path(__file__).resolve().parent / "bot.log", encoding="utf-8"
            ),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def format_user_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "The media server took too long to respond. Please try again in a moment."
    if isinstance(exc, httpx.ConnectError):
        return "Unable to reach the media server right now. Please try again shortly."

    msg = f"{type(exc).__name__} {exc}".lower()
    if "50 mb" in msg or "exceeds telegram" in msg or "safety size limit" in msg:
        return "This file exceeds Telegram's 50 MB upload limit. Try a shorter track or lower quality."
    if "403" in msg or "rejected" in msg or "upstream" in msg or "blocked" in msg:
        return "This track is restricted or temporarily unavailable on the source server. Try another title or version."
    if "429" in msg or "rate limit" in msg:
        return "Too many requests right now. Please wait a few seconds and try again."
    if "timeout" in msg or "timed out" in msg:
        return "The media server took too long to respond. Please try again in a moment."
    if "no matching" in msg or "no results" in msg:
        return "No matching song or video was found. Try a different title or artist."
    if "no downloadable" in msg or "no mp4 format" in msg or "no formats" in msg:
        return "No downloadable format is available for this link."
    if "network" in msg or "connect" in msg or "500" in msg or "502" in msg or "503" in msg or "404" in msg:
        return "Unable to reach the media server right now. Please try again shortly."
    if "ffmpeg" in msg or "merge" in msg or "convert" in msg:
        return "Could not process the audio or video stream. Please try another version."
    if isinstance(exc, DownloadError):
        clean = str(exc).split(":", 1)[0].strip()
        if clean and len(clean) <= 100 and "http" not in clean.lower():
            return clean
    return "Unable to complete the request right now. Please try again shortly."


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def extract_query(message: Message) -> str:
    text = (message.text or "").strip()
    query = ""
    if message.entities:
        first = message.entities[0]
        if first.type == MessageEntity.BOT_COMMAND and first.offset == 0:
            query = text[first.length :].strip()
    if not query:
        m = _BANG_CMD_RE.match(text)
        if m:
            query = (m.group(1) or "").strip()
    if not query and message.reply_to_message:
        query = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        ).strip()
    return query


def store_search_cache(
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    candidates: Optional[list[SongCandidate]] = None,
) -> str:
    cache: dict[str, dict] = context.bot_data.setdefault("searches", {})
    if len(cache) > 300:
        oldest = next(iter(cache))
        cache.pop(oldest, None)
    sid = uuid.uuid4().hex[:8]
    cache[sid] = {"query": query, "candidates": candidates or []}
    return sid


def get_search_cache(
    context: ContextTypes.DEFAULT_TYPE, sid: str
) -> Optional[dict]:
    cache: dict[str, dict] = context.bot_data.get("searches", {})
    return cache.get(sid)


async def safe_edit_html(
    msg: Message,
    new_html: str,
    last_text_holder: list[str],
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if last_text_holder and last_text_holder[0] == new_html and reply_markup is None:
        return
    try:
        await msg.edit_text(
            new_html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        if last_text_holder:
            last_text_holder[0] = new_html
        else:
            last_text_holder.append(new_html)
    except RetryAfter as exc:
        logger.debug("Rate limited editing message: retry after %s", exc.retry_after)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.debug("edit_text skipped: %s", exc)
    except Exception as exc:
        logger.debug("edit_text error: %s", exc)


def format_user_mention(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    display_name = (
        user.full_name
        or user.first_name
        or (f"@{user.username}" if user.username else "User")
    ).strip()
    return f'<a href="tg://user?id={user.id}">{html.escape(display_name)}</a>'


def build_media_caption(
    track: DownloadedTrack,
    requester: Optional[User] = None,
) -> str:
    size_mb = track.size_bytes / (1024 * 1024)
    dur_str = format_duration(track.duration_seconds)
    safe_title = html.escape(track.title[:160])
    safe_url = html.escape(track.source_url, quote=True)

    lines: list[str] = []
    if track.performer and track.performer != "Unknown Artist":
        role = "Artist" if track.media_type == "audio" else "Channel"
        lines.append(f"👤 <b>{role}:</b> {html.escape(track.performer[:100])}")
    lines.append(f"⏱ <b>Duration:</b> <code>{dur_str}</code>")

    if track.media_type == "video":
        res = f" ({track.width}×{track.height})" if track.width and track.height else ""
        lines.append(
            f"🎞 <b>Quality:</b> <code>{html.escape(track.format_label)}{res}</code>"
        )
    else:
        lines.append("🎧 <b>Format:</b> <code>MP3 Audio</code>")

    lines.append(f"📦 <b>Size:</b> <code>{size_mb:.2f} MB</code>")

    mention = format_user_mention(requester)
    if mention:
        lines.append(f"🙋 <b>Downloaded by:</b> {mention}")

    icon = "🎵" if track.media_type == "audio" else "🎬"
    quote_block = "\n".join(lines)
    return (
        f"{icon} <b>{safe_title}</b>\n\n"
        f"<blockquote>{quote_block}</blockquote>\n"
        f"🔗 <a href=\"{safe_url}\">Watch on YouTube</a>"
    )


def build_post_media_keyboard(
    track: DownloadedTrack,
    search_id: Optional[str] = None,
) -> Optional[InlineKeyboardMarkup]:
    vid = extract_youtube_id(track.source_url)
    if not vid:
        return None
    if track.media_type == "audio":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎬 Download Video", callback_data=f"dlv:hd:{vid}")]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎵 Download Audio", callback_data=f"dla:{vid}")]]
    )


def build_search_results_markup(
    candidates: list[SongCandidate],
    search_id: str,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🔍 <b>Search Results</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(candidates, start=1):
        dur = f" · <code>{html.escape(item.duration_text)}</code>" if item.duration_text else ""
        short_title = item.title[:54] + ("…" if len(item.title) > 54 else "")
        chan = f" — <i>{html.escape(item.channel[:28])}</i>" if item.channel else ""
        lines.append(f"<b>{idx}.</b> {html.escape(short_title)}{chan}{dur}")

        btn_dur = f" ({item.duration_text})" if item.duration_text else ""
        btn_title = item.title[:32] + ("…" if len(item.title) > 32 else "")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{idx}. {btn_title}{btn_dur}",
                    callback_data=f"pick:{search_id}:{idx - 1}",
                )
            ]
        )
    lines.append("\n<i>Tap any track below to choose Audio or Video.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def deliver_media(
    target_message: Message,
    candidates: list[SongCandidate],
    settings: Settings,
    status_msg: Message,
    last_text: list[str],
    mode: str = "audio",
    video_quality: str = "hd",
    search_id: Optional[str] = None,
    requester: Optional[User] = None,
) -> None:
    async def update_status(msg_html: str) -> None:
        await safe_edit_html(status_msg, msg_html, last_text)

    chat_id = target_message.chat_id
    bot = target_message.get_bot()
    action = (
        ChatAction.UPLOAD_DOCUMENT if mode == "audio" else ChatAction.UPLOAD_VIDEO
    )

    async def keep_action(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                continue

    stop_event = asyncio.Event()
    action_task = asyncio.create_task(keep_action(stop_event))

    try:
        with tempfile.TemporaryDirectory(prefix="tg_media_") as tmp:
            work_dir = Path(tmp)
            if mode == "video":
                track = await download_video(
                    candidates=candidates,
                    work_dir=work_dir,
                    api_base=settings.api_base_url,
                    max_bytes=settings.max_telegram_bytes,
                    quality=video_quality,
                    status_callback=update_status,
                )
            else:
                track = await download_song(
                    candidates=candidates,
                    work_dir=work_dir,
                    api_base=settings.api_base_url,
                    max_bytes=settings.max_telegram_bytes,
                    status_callback=update_status,
                )

            caption = build_media_caption(track, requester=requester)
            markup = build_post_media_keyboard(track, search_id=search_id)
            size_mb = track.size_bytes / (1024 * 1024)

            logger.info(
                "Uploading %s to chat_id=%s: %s (%.2f MB, format=%s)",
                track.media_type,
                chat_id,
                track.filename,
                size_mb,
                track.format_label,
            )

            thumb_fp = (
                track.thumbnail_path.open("rb")
                if track.thumbnail_path and track.thumbnail_path.exists()
                else None
            )
            try:
                with track.file_path.open("rb") as media_fp:
                    if track.media_type == "video":
                        await target_message.reply_video(
                            video=media_fp,
                            filename=track.filename,
                            duration=track.duration_seconds,
                            width=track.width,
                            height=track.height,
                            thumbnail=thumb_fp,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            supports_streaming=True,
                            reply_markup=markup,
                            write_timeout=180,
                        )
                    else:
                        await target_message.reply_audio(
                            audio=media_fp,
                            filename=track.filename,
                            title=track.title[:128],
                            performer=track.performer[:128],
                            duration=track.duration_seconds,
                            thumbnail=thumb_fp,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=markup,
                            write_timeout=180,
                        )
            finally:
                if thumb_fp is not None:
                    thumb_fp.close()

            logger.info(
                "Sent %s successfully to chat_id=%s: %s",
                track.media_type,
                chat_id,
                track.filename,
            )

        try:
            await status_msg.delete()
        except Exception:
            pass
    finally:
        stop_event.set()
        action_task.cancel()
        try:
            await action_task
        except asyncio.CancelledError:
            pass


def _welcome_card_html() -> str:
    return (
        "🎧 <b>Song Control</b>\n\n"
        "<blockquote>• <code>/song &lt;name&gt;</code>  ·  MP3 Audio\n"
        "• <code>!song &lt;name&gt;</code>  ·  MP3 Audio\n"
        "• <code>/video &lt;name&gt;</code>  ·  HD MP4 Video\n"
        "• <code>!video &lt;name&gt;</code>  ·  HD MP4 Video</blockquote>\n\n"
        "<i>You can also pass a YouTube link instead of</i> <code>&lt;name&gt;</code>.\n\n"
        "⚡ <i>Powered by</i> <b>RààDBhai_FF</b>"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Keep /start silent as requested; media/search commands remain active.
    return


async def _run_media_command(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    mode: str,
    requester: Optional[User] = None,
) -> None:
    settings: Settings = context.bot_data["settings"]
    icon = "🎧" if mode == "audio" else "🎬"
    header = "Downloading Audio" if mode == "audio" else "Downloading Video"
    initial_html = (
        f"{icon} <b>{header}</b>\n\n"
        f"<blockquote>🔍 <b>Query:</b> <code>{html.escape(query[:80])}</code></blockquote>\n"
        f"⠋ <i>Searching YouTube...</i>\n"
        f"<code>▰▰▱▱▱▱▱▱▱▱</code>  <b>15%</b>"
    )
    status_msg = await message.reply_text(
        initial_html,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    last_text = [initial_html]

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING
        )
    except Exception:
        pass

    try:
        if is_url(query):
            candidates = [await get_url_metadata(query)]
            sid = None
        else:
            candidates = await search_youtube(query, limit=5)
            sid = store_search_cache(context, query, candidates) if len(candidates) > 1 else None

        logger.info(
            "Found %d candidate(s) for %r (mode=%s)%s",
            len(candidates),
            query,
            mode,
            f" [top={candidates[0].video_id}]" if candidates else "",
        )

        if not candidates:
            await safe_edit_html(
                status_msg,
                f"❌ <b>No results found</b> for <code>{html.escape(query)}</code>.\n"
                f"<i>Try a clearer title or artist name.</i>",
                last_text,
            )
            return

        await deliver_media(
            target_message=message,
            candidates=candidates,
            settings=settings,
            status_msg=status_msg,
            last_text=last_text,
            mode=mode,
            video_quality="hd",
            search_id=sid,
            requester=requester or message.from_user,
        )
    except DownloadError as exc:
        logger.warning("%s download failed for %r: %s", mode, query, exc)
        await safe_edit_html(
            status_msg,
            f"❌ <b>Download failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>",
            last_text,
        )
    except Exception as exc:
        logger.exception("Unexpected error in /%s for %r", mode, query)
        await safe_edit_html(
            status_msg,
            f"❌ <b>Download failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>",
            last_text,
        )


def _linked_user_account_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    controller = context.bot_data.get("user_account_controller")
    return bool(controller and controller.status().get("connected"))


async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _linked_user_account_active(context):
        return
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    query = extract_query(message)
    logger.info(
        "Received song command from user_id=%s (@%s) chat_id=%s query=%r",
        user.id if user else None,
        user.username if user else None,
        message.chat_id,
        query,
    )
    if not query:
        return

    await _run_media_command(
        message, context, query, mode="audio", requester=user
    )


async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _linked_user_account_active(context):
        return
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    query = extract_query(message)
    logger.info(
        "Received video command from user_id=%s (@%s) chat_id=%s query=%r",
        user.id if user else None,
        user.username if user else None,
        message.chat_id,
        query,
    )
    if not query:
        return

    await _run_media_command(
        message, context, query, mode="video", requester=user
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _linked_user_account_active(context):
        return
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    query = extract_query(message)
    logger.info(
        "Received search command from user_id=%s (@%s) query=%r",
        user.id if user else None,
        user.username if user else None,
        query,
    )
    if not query:
        return

    status_msg = await message.reply_text(
        f"🔍 <b>Searching</b>  ·  <code>{html.escape(query)}</code>",
        parse_mode=ParseMode.HTML,
    )
    try:
        results = await search_youtube(query, limit=5)
        if not results:
            await status_msg.edit_text(
                f"❌ <b>No results found</b> for <code>{html.escape(query)}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        sid = store_search_cache(context, query, results)
        text_html, markup = build_search_results_markup(results, sid)
        await status_msg.edit_text(
            text_html,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Search command failed for %r", query)
        await status_msg.edit_text(
            f"❌ <b>Search failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>",
            parse_mode=ParseMode.HTML,
        )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    if not cb or not cb.data or not cb.message:
        return

    data = cb.data
    try:
        await cb.answer()
    except BadRequest as exc:
        logger.warning("cb.answer skipped (%s); continuing", exc)
    except Exception as exc:
        logger.warning("cb.answer error (%s); continuing", exc)

    settings: Settings = context.bot_data["settings"]
    msg = cb.message

    if data.startswith("dla:") or data.startswith("dlv:"):
        if data.startswith("dla:"):
            vid = data.split(":", 1)[1]
            mode = "audio"
            quality = "hd"
        else:
            _, quality, vid = data.split(":", 2)
            mode = "video"

        logger.info("Callback %s triggered in chat_id=%s", data, msg.chat_id)
        url = f"https://www.youtube.com/watch?v={vid}"
        icon = "🎧" if mode == "audio" else "🎬"
        header = "Downloading Audio" if mode == "audio" else "Downloading Video"
        initial_html = (
            f"{icon} <b>{header}</b>\n\n"
            f"⠋ <i>Preparing stream...</i>\n"
            f"<code>▰▰▱▱▱▱▱▱▱▱</code>  <b>15%</b>"
        )
        status_msg = await msg.reply_text(initial_html, parse_mode=ParseMode.HTML)
        last_text = [initial_html]
        try:
            candidate = await get_url_metadata(url)
            await deliver_media(
                target_message=msg,
                candidates=[candidate],
                settings=settings,
                status_msg=status_msg,
                last_text=last_text,
                mode=mode,
                video_quality=quality,
                requester=cb.from_user,
            )
        except DownloadError as exc:
            await safe_edit_html(
                status_msg,
                f"❌ <b>Download failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>",
                last_text,
            )
        except Exception as exc:
            logger.exception("Callback download failed for %s", data)
            await safe_edit_html(
                status_msg,
                f"❌ <b>Download failed</b>\n\n<blockquote>{html.escape(format_user_error(exc))}</blockquote>",
                last_text,
            )
        return

    if data.startswith("alt:"):
        sid = data.split(":", 1)[1]
        entry = get_search_cache(context, sid)
        if not entry or not entry.get("candidates"):
            await msg.reply_text(
                "⌛ <b>Session expired.</b> Search again with <code>/song</code> or <code>/search</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        text_html, markup = build_search_results_markup(entry["candidates"], sid)
        await msg.reply_text(
            text_html,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        return

    if data.startswith("pick:"):
        _, sid, idx_str = data.split(":", 2)
        entry = get_search_cache(context, sid)
        if not entry or not entry.get("candidates"):
            await msg.reply_text(
                "⌛ <b>Session expired.</b> Try <code>/search</code> again.",
                parse_mode=ParseMode.HTML,
            )
            return
        idx = int(idx_str)
        candidates: list[SongCandidate] = entry["candidates"]
        if not (0 <= idx < len(candidates)):
            return
        item = candidates[idx]
        dur = item.duration_text or "—"
        card_html = (
            f"🎵 <b>{html.escape(item.title[:140])}</b>\n\n"
            f"<blockquote>👤 <b>Channel:</b> {html.escape(item.channel or 'YouTube')}\n"
            f"⏱ <b>Duration:</b> <code>{html.escape(dur)}</code></blockquote>"
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎵 Download Audio", callback_data=f"dla:{item.video_id}"),
                    InlineKeyboardButton("🎬 Download Video", callback_data=f"dlv:hd:{item.video_id}"),
                ],
                [InlineKeyboardButton("← Back", callback_data=f"alt:{sid}")],
            ]
        )
        if item.thumbnail_url:
            try:
                await msg.reply_photo(
                    photo=item.thumbnail_url,
                    caption=card_html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return
            except Exception:
                pass
        await msg.reply_text(
            card_html,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return

    if data.startswith("qdl:"):
        _, action_type, sid = data.split(":", 2)
        entry = get_search_cache(context, sid)
        if not entry:
            await msg.reply_text(
                "⌛ <b>Session expired.</b> Use <code>/song</code> or <code>/video</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        query = entry["query"]
        target_msg = msg.reply_to_message or msg
        if action_type == "a":
            await _run_media_command(
                target_msg, context, query, mode="audio", requester=cb.from_user
            )
        elif action_type == "v":
            await _run_media_command(
                target_msg, context, query, mode="video", requester=cb.from_user
            )
        elif action_type == "s":
            results = await search_youtube(query, limit=5)
            if not results:
                await msg.reply_text(
                    "❌ <b>No results found.</b>",
                    parse_mode=ParseMode.HTML,
                )
                return
            entry["candidates"] = results
            text_html, markup = build_search_results_markup(results, sid)
            await msg.reply_text(
                text_html,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )


async def private_text_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Plain text is never an implicit search/download request.
    return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram bot error: %s", context.error)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("song", "Download MP3 audio (/song or !song)"),
        BotCommand("video", "Download MP4 video (/video or !video)"),
        BotCommand("search", "Search tracks and pick format"),
    ]
    await application.bot.set_my_commands(commands)
    me = await application.bot.get_me()
    logger.info("Bot ready: @%s (id=%s)", me.username, me.id)


def build_application(settings: Optional[Settings] = None) -> Application:
    cfg = settings or get_settings()
    defaults = Defaults(do_quote=True, allow_sending_without_reply=True)
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .defaults(defaults)
        .connect_timeout(20.0)
        .read_timeout(60.0)
        .write_timeout(120.0)
        .media_write_timeout(180.0)
        .post_init(post_init)
        .build()
    )
    app.bot_data["settings"] = cfg

    app.add_handler(CommandHandler(["start", "help"], start_command))
    app.add_handler(CommandHandler("song", song_command))
    app.add_handler(CommandHandler("video", video_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(re.compile(r"^!song(?:\s|$|@)", re.IGNORECASE)),
            song_command,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(re.compile(r"^!video(?:\s|$|@)", re.IGNORECASE)),
            video_command,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(re.compile(r"^!search(?:\s|$|@)", re.IGNORECASE)),
            search_command,
        )
    )
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_text_hint))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    setup_logging()
    settings = get_settings()
    app = build_application(settings)
    logger.info("Starting Telegram Song & Video Bot polling...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
