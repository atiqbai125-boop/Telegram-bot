from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from types import SimpleNamespace
from telegram import MessageEntity

from bot import (
    _BANG_CMD_RE,
    build_application,
    build_media_caption,
    build_post_media_keyboard,
    extract_query,
    format_duration,
    format_user_error,
    format_user_mention,
)
from services.downloader import DownloadedTrack, DownloadError, UpstreamRejectedError
from services.formatting import build_media_caption_html, build_userbot_search_html, format_user_error as format_userbot_error
from services.user_account import (
    COMMAND_RE as USER_ACCOUNT_COMMAND_RE,
    PICK_RE as USER_ACCOUNT_PICK_RE,
    is_authorized_trigger,
)
from config import get_settings
from services.downloader import (
    choose_audio_candidates,
    choose_video_plans,
    detect_media_extension,
    download_song,
    download_video,
    sanitize_filename,
)
from services.youtube_search import (
    extract_youtube_id,
    get_url_metadata,
    parse_duration_seconds,
    search_youtube,
    split_title_artist,
)


def test_helpers() -> None:
    assert extract_youtube_id("https://youtu.be/JGwWNGJdvx8") == "JGwWNGJdvx8"
    assert (
        extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10")
        == "dQw4w9WgXcQ"
    )
    assert parse_duration_seconds("4:24") == 264
    assert parse_duration_seconds("1:02:03") == 3723
    assert parse_duration_seconds("LIVE") is None
    assert format_duration(264) == "04:24"
    assert format_duration(3723) == "01:02:03"

    title, artist = split_title_artist(
        "Ed Sheeran - Shape of You (Official Music Video)", "Ed Sheeran"
    )
    assert title == "Shape of You (Official Music Video)"
    assert artist == "Ed Sheeran"

    assert sanitize_filename('আমার: "সোনার" / বাংলা?', ".mp3") == "আমার সোনার বাংলা.mp3"
    assert detect_media_extension(b"ID3\x03\x00") == ".mp3"
    assert detect_media_extension(b"\x00\x00\x00\x18ftypdash", "audio/mp4") == ".m4a"
    assert detect_media_extension(b"\x00\x00\x00\x18ftypmp42", "video/mp4") == ".mp4"
    assert detect_media_extension(b"\x1a\x45\xdf\xa3\x9f") == ".webm"

    sample_formats = [
        {"fmt": 0, "kind": "video+audio", "label": "360pMP4", "size": ""},
        {"fmt": 5, "kind": "video-only", "label": "1080pMP4", "size": "77.16 MB"},
        {"fmt": 7, "kind": "video-only", "label": "720pMP4", "size": "25.23 MB"},
        {"fmt": 9, "kind": "video-only", "label": "480pMP4", "size": "13.45 MB"},
        {"fmt": 21, "kind": "audio-only", "label": "MP3·320kbps", "size": "≈ 8.13 MB"},
        {"fmt": 17, "kind": "audio-only", "label": "M4AAudio·131kbps", "size": "4.06 MB"},
    ]
    ranked_audio = choose_audio_candidates(
        sample_formats, has_ffmpeg=True, max_bytes=49 * 1024 * 1024
    )
    assert ranked_audio[0]["fmt"] == 17

    hd_plans = choose_video_plans(
        sample_formats, has_ffmpeg=True, max_bytes=49 * 1024 * 1024, quality="hd"
    )
    assert hd_plans[0]["mode"] == "mux"
    assert hd_plans[0]["video"]["fmt"] == 7  # 720pMP4 (1080p skipped > 49MB)
    assert hd_plans[-1]["mode"] == "combined"

    # When 1080p fits under 49MB, HD picks 1080p first
    short_video_formats = [
        {"fmt": 5, "kind": "video-only", "label": "1080pMP4", "size": "32.5 MB"},
        {"fmt": 7, "kind": "video-only", "label": "720pMP4", "size": "18.1 MB"},
        {"fmt": 17, "kind": "audio-only", "label": "M4AAudio·131kbps", "size": "3.2 MB"},
    ]
    short_hd_plans = choose_video_plans(
        short_video_formats, has_ffmpeg=True, max_bytes=49 * 1024 * 1024, quality="hd"
    )
    assert short_hd_plans[0]["video"]["fmt"] == 5
    assert short_hd_plans[0]["label"] == "1080p Full HD"

    sd_plans = choose_video_plans(
        sample_formats, has_ffmpeg=True, max_bytes=49 * 1024 * 1024, quality="sd"
    )
    assert sd_plans[0]["mode"] == "combined"

    app = build_application()
    assert app is not None
    assert app.bot.defaults.do_quote is True
    assert app.bot.defaults.allow_sending_without_reply is True

    raw_err = UpstreamRejectedError('HTTP 502: {"error":"Upstream download failed with HTTP 403. Replace SNAPYT_PROXY"}')
    user_reason = format_user_error(raw_err)
    assert "SNAPYT_PROXY" not in user_reason and "502" not in user_reason
    assert "restricted" in user_reason.lower()

    import httpx
    assert "too long" in format_user_error(httpx.ReadTimeout("")).lower()
    assert "50 mb" in format_user_error(DownloadError("Converted MP3 exceeds Telegram's 50 MB limit.")).lower()

    # Test /song, !song, /video, !video extraction
    m_slash = SimpleNamespace(
        text="/Song Shape of You",
        entities=[SimpleNamespace(offset=0, length=5, type=MessageEntity.BOT_COMMAND)],
        reply_to_message=None,
    )
    assert extract_query(m_slash) == "Shape of You"

    m_bang_song = SimpleNamespace(
        text="!song আমার সোনার বাংলা",
        entities=[],
        reply_to_message=None,
    )
    assert extract_query(m_bang_song) == "আমার সোনার বাংলা"

    m_bang_url = SimpleNamespace(
        text="!video https://youtu.be/JGwWNGJdvx8",
        entities=[SimpleNamespace(offset=7, length=28, type=MessageEntity.URL)],
        reply_to_message=None,
    )
    assert extract_query(m_bang_url) == "https://youtu.be/JGwWNGJdvx8"

    m_bang_empty = SimpleNamespace(text="!Video", entities=[], reply_to_message=None)
    assert extract_query(m_bang_empty) == ""

    assert _BANG_CMD_RE.match("!song test")
    assert _BANG_CMD_RE.match("!Video test")
    assert not _BANG_CMD_RE.match("!songs test")

    mock_user = SimpleNamespace(id=7832264582, full_name="RààDBhai", first_name="RààDBhai", username="AuraKnife")
    assert format_user_mention(mock_user) == '<a href="tg://user?id=7832264582">RààDBhai</a>'
    dummy_track = DownloadedTrack(
        file_path=Path("dummy.mp3"),
        filename="dummy.mp3",
        title="Shape of You",
        performer="Ed Sheeran",
        duration_seconds=264,
        thumbnail_path=None,
        format_label="MP3 Audio",
        source_url="https://www.youtube.com/watch?v=JGwWNGJdvx8",
        size_bytes=6_000_000,
        media_type="audio",
    )
    cap_with_user = build_media_caption(dummy_track, requester=mock_user)
    assert "Downloaded by:" in cap_with_user and 'tg://user?id=7832264582' in cap_with_user

    userbot_caption = build_media_caption_html(
        dummy_track, requester_id=7832264582, requester_name="RààDBhai"
    )
    assert "Downloaded by:" in userbot_caption and "For HD video" in userbot_caption
    assert "tg://user?id=7832264582" in userbot_caption
    assert "reply with" in build_userbot_search_html([]).lower()
    assert USER_ACCOUNT_COMMAND_RE.match("!SONG query")
    assert USER_ACCOUNT_COMMAND_RE.match("/Video query")
    assert not USER_ACCOUNT_COMMAND_RE.match("/start")
    assert not USER_ACCOUNT_COMMAND_RE.match("ordinary text")
    assert USER_ACCOUNT_PICK_RE.match("2v") and not USER_ACCOUNT_PICK_RE.match("9")
    assert is_authorized_trigger(100, 100, False, None)  # Owner can command anywhere.
    assert is_authorized_trigger(200, 100, True, 100)  # Others must reply to owner.
    assert not is_authorized_trigger(200, 100, False, None)
    assert not is_authorized_trigger(200, 100, True, 300)
    assert "login code is incorrect" in format_userbot_error(
        type("PhoneCodeInvalidError", (Exception,), {})()
    ).lower()

    print("[PASS] Helper & UI unit checks")


async def test_live_pipeline() -> None:
    settings = get_settings()

    en_results = await search_youtube("Shape of You Ed Sheeran", limit=3)
    assert len(en_results) >= 1
    print(f"[PASS] EN search: {en_results[0].title} ({en_results[0].duration_text})")

    bn_results = await search_youtube("সে আমারে", limit=4)
    assert len(bn_results) >= 2
    print(f"[PASS] BN search: {bn_results[0].title} ({bn_results[0].duration_text})")

    meta = await get_url_metadata("https://youtu.be/JGwWNGJdvx8")
    assert meta.video_id == "JGwWNGJdvx8"

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "audio").mkdir(parents=True, exist_ok=True)
        audio_track = await download_song(
            candidates=en_results,
            work_dir=work / "audio",
            api_base=settings.api_base_url,
            max_bytes=settings.max_telegram_bytes,
        )
        assert audio_track.file_path.exists()
        assert audio_track.file_path.read_bytes()[:3] == b"ID3"
        cap = build_media_caption(audio_track)
        assert "<blockquote>" in cap and "MP3 Audio" in cap
        kb = build_post_media_keyboard(audio_track)
        assert kb is not None
        assert len(kb.inline_keyboard) == 1 and len(kb.inline_keyboard[0]) == 1
        assert kb.inline_keyboard[0][0].text == "🎬 Download Video"
        assert kb.inline_keyboard[0][0].callback_data.startswith("dlv:hd:")
        print(
            f"[PASS] Audio MP3: {audio_track.filename} "
            f"({audio_track.size_bytes / (1024*1024):.2f} MB)"
        )

        # Test Video HD (up to 1080p/720p mux)
        (work / "video").mkdir(parents=True, exist_ok=True)
        video_track = await download_video(
            candidates=en_results,
            work_dir=work / "video",
            api_base=settings.api_base_url,
            max_bytes=settings.max_telegram_bytes,
            quality="hd",
        )
        assert video_track.file_path.exists()
        assert video_track.media_type == "video"
        assert b"ftyp" in video_track.file_path.read_bytes()[:16]
        assert video_track.width and video_track.height
        v_cap = build_media_caption(video_track)
        assert "🎬" in v_cap and "<blockquote>" in v_cap
        v_kb = build_post_media_keyboard(video_track)
        assert v_kb is not None
        assert len(v_kb.inline_keyboard) == 1 and len(v_kb.inline_keyboard[0]) == 1
        assert v_kb.inline_keyboard[0][0].text == "🎵 Download Audio"
        print(
            f"[PASS] Video MP4: {video_track.filename} "
            f"({video_track.size_bytes / (1024*1024):.2f} MB, "
            f"{video_track.width}x{video_track.height}, {video_track.format_label})"
        )


def main() -> None:
    test_helpers()
    asyncio.run(test_live_pipeline())


if __name__ == "__main__":
    main()
