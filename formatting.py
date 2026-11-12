from __future__ import annotations

import html
from typing import Optional

import httpx

from services.downloader import DownloadError, DownloadedTrack
from services.youtube_search import SongCandidate


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_user_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "The media server took too long to respond. Please try again in a moment."
    if isinstance(exc, httpx.ConnectError):
        return "Unable to reach the media server right now. Please try again shortly."

    error_type = type(exc).__name__.lower()
    msg = f"{type(exc).__name__} {exc}".lower()
    if "phonecodeinvalid" in error_type or "phone_code_invalid" in msg:
        return "That Telegram login code is incorrect. Check the latest code and try again."
    if "phonecodeexpired" in error_type or "phone_code_expired" in msg:
        return "That Telegram login code has expired. Start linking again to request a new code."
    if "passwordhashinvalid" in error_type or "password_hash_invalid" in msg:
        return "That two-step verification password was not accepted. Check it and try again."
    if "phonenumberinvalid" in error_type or "phone_number_invalid" in msg:
        return "Telegram did not accept that phone number. Include the country code and try again."
    if "apiidinvalid" in error_type or "api_id_invalid" in msg or "api_hash_invalid" in msg:
        return "Telegram did not accept the API ID or hash. Check the details and try again."
    if "floodwait" in error_type or "flood_wait" in msg or "too many requests" in msg:
        return "Telegram is limiting sign-in attempts. Wait a little while before trying again."
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


def build_media_caption_html(
    track: DownloadedTrack,
    requester_id: Optional[int] = None,
    requester_name: Optional[str] = None,
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

    if requester_id is not None and requester_name:
        mention = f'<a href="tg://user?id={requester_id}">{html.escape(requester_name)}</a>'
        lines.append(f"🙋 <b>Downloaded by:</b> {mention}")

    icon = "🎵" if track.media_type == "audio" else "🎬"
    quote_block = "\n".join(lines)
    alternate = (
        "🎬 For HD video, send <code>/video &lt;name&gt;</code>."
        if track.media_type == "audio"
        else "🎵 For MP3 audio, send <code>/song &lt;name&gt;</code>."
    )
    return (
        f"{icon} <b>{safe_title}</b>\n\n"
        f"<blockquote>{quote_block}</blockquote>\n"
        f"🔗 <a href=\"{safe_url}\">Watch on YouTube</a>\n"
        f"{alternate}"
    )


def build_userbot_search_html(candidates: list[SongCandidate]) -> str:
    lines = ["🔍 <b>Search Results</b>\n"]
    quote_lines: list[str] = []
    for idx, item in enumerate(candidates, start=1):
        dur = f" · <code>{html.escape(item.duration_text)}</code>" if item.duration_text else ""
        short_title = item.title[:54] + ("…" if len(item.title) > 54 else "")
        chan = f" — <i>{html.escape(item.channel[:26])}</i>" if item.channel else ""
        quote_lines.append(f"<b>{idx}.</b> {html.escape(short_title)}{chan}{dur}")

    lines.append(f"<blockquote>{chr(10).join(quote_lines)}</blockquote>\n")
    lines.append(
        "<i>Reply with</i> <code>1</code>–<code>5</code> <i>for MP3 Audio or</i> "
        "<code>1v</code>–<code>5v</code> <i>for HD Video.</i>"
    )
    return "\n".join(lines)
