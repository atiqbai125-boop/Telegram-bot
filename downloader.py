from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from services.youtube_search import SongCandidate, split_title_artist

logger = logging.getLogger(__name__)

StatusCallback = Optional[Callable[[str], Awaitable[None]]]
ProgressCallback = Optional[Callable[[int, Optional[int]], Awaitable[None]]]

_SPINNERS = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class DownloadError(RuntimeError):
    """Raised when a track or video cannot be fetched or processed."""


class UpstreamRejectedError(DownloadError):
    """Raised when upstream explicitly rejects a video (e.g. HTTP 403)."""


@dataclass
class DownloadedTrack:
    file_path: Path
    filename: str
    title: str
    performer: str
    duration_seconds: Optional[int]
    thumbnail_path: Optional[Path]
    format_label: str
    source_url: str
    size_bytes: int
    media_type: str = "audio"  # "audio" or "video"
    width: Optional[int] = None
    height: Optional[int] = None


def find_ffmpeg_exe() -> Optional[str]:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def sanitize_filename(name: str, ext: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "media"
    if len(cleaned) > 85:
        cleaned = cleaned[:85].rstrip(" .")
    ext_clean = ext if ext.startswith(".") else f".{ext}"
    return f"{cleaned}{ext_clean}"


def parse_size_bytes(size_text: str) -> Optional[int]:
    if not size_text:
        return None
    m = re.search(r"([\d.]+)\s*(KB|MB|GB|B)", size_text, flags=re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit]
    return int(val * mult)


def detect_media_extension(header_bytes: bytes, content_type: str = "") -> str:
    if header_bytes.startswith(b"ID3") or (
        len(header_bytes) >= 2
        and header_bytes[0] == 0xFF
        and (header_bytes[1] & 0xE0) == 0xE0
    ):
        return ".mp3"
    if b"ftyp" in header_bytes[:16]:
        ct = content_type.lower()
        return ".mp4" if "video" in ct else ".m4a"
    if header_bytes.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if header_bytes.startswith(b"OggS"):
        return ".ogg"
    ct = content_type.lower()
    if "video/mp4" in ct:
        return ".mp4"
    if "mp4" in ct or "m4a" in ct:
        return ".m4a"
    if "webm" in ct:
        return ".webm"
    if "ogg" in ct:
        return ".ogg"
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    return ".bin"


def choose_audio_candidates(
    formats: list[dict],
    has_ffmpeg: bool,
    max_bytes: int,
) -> list[dict]:
    audio_only = [
        f
        for f in formats
        if "audio" in str(f.get("kind", "")).lower()
        and "video" not in str(f.get("kind", "")).lower()
    ]
    video_audio = [
        f for f in formats if "video+audio" in str(f.get("kind", "")).lower()
    ]

    def bitrate_score(label: str) -> int:
        m = re.search(r"(\d+)\s*kbps", label, flags=re.IGNORECASE)
        return int(m.group(1)) if m else 0

    def rank_key(fmt: dict) -> tuple[int, int]:
        label = str(fmt.get("label", ""))
        low = label.lower()
        bps = bitrate_score(label)
        if "m4a" in low:
            tier = 0
        elif "mp3" in low:
            tier = 1
        elif "opus" in low:
            tier = 2
        else:
            tier = 3
        return (tier, -bps)

    def within_limit(fmt: dict) -> bool:
        est = parse_size_bytes(str(fmt.get("size", "")))
        return est is None or est <= max_bytes

    audio_sorted = sorted(audio_only, key=rank_key)
    preferred = [f for f in audio_sorted if within_limit(f)]
    if not preferred:
        preferred = audio_sorted

    if has_ffmpeg:
        va_ok = [f for f in video_audio if within_limit(f)]
        preferred.extend(va_ok)

    return preferred


def choose_video_plans(
    formats: list[dict],
    has_ffmpeg: bool,
    max_bytes: int,
    quality: str = "hd",
) -> list[dict]:
    combined = [
        f for f in formats if "video+audio" in str(f.get("kind", "")).lower()
    ]
    video_only = [
        f for f in formats if "video-only" in str(f.get("kind", "")).lower()
    ]
    audio_only = [
        f
        for f in formats
        if "audio" in str(f.get("kind", "")).lower()
        and "video" not in str(f.get("kind", "")).lower()
    ]

    plans: list[dict] = []

    def res_height(label: str) -> int:
        m = re.search(r"(\d{3,4})p", label, flags=re.IGNORECASE)
        return int(m.group(1)) if m else 0

    def est_bytes(fmt: dict) -> Optional[int]:
        return parse_size_bytes(str(fmt.get("size", "")))

    if quality == "hd" and has_ffmpeg and video_only and audio_only:
        m4a_first = sorted(
            audio_only,
            key=lambda f: (0 if "m4a" in str(f.get("label", "")).lower() else 1),
        )
        best_audio = m4a_first[0]
        audio_size = est_bytes(best_audio) or (4 * 1024 * 1024)

        mp4_videos = [
            f
            for f in video_only
            if "mp4" in str(f.get("label", "")).lower()
            and "av1" not in str(f.get("label", "")).lower()
            and 360 <= res_height(str(f.get("label", ""))) <= 1080
        ]
        # Highest compatible H.264 MP4 quality fitting under Telegram's 50 MB limit
        pref_order = {1080: 0, 720: 1, 480: 2, 360: 3}
        mp4_videos.sort(
            key=lambda f: (
                pref_order.get(res_height(str(f.get("label", ""))), 9),
                -(est_bytes(f) or 0),
            )
        )

        for vf in mp4_videos:
            v_size = est_bytes(vf)
            if v_size is not None and (v_size + audio_size) > max_bytes:
                continue
            h = res_height(str(vf.get("label", "")))
            label = f"{h}p Full HD" if h >= 1080 else (f"{h}p HD" if h >= 720 else f"{h}p MP4")
            plans.append(
                {
                    "mode": "mux",
                    "video": vf,
                    "audio": best_audio,
                    "label": label,
                    "expected_bytes": v_size,
                }
            )

    for cf in combined:
        c_size = est_bytes(cf)
        if c_size is not None and c_size > max_bytes:
            continue
        h = res_height(str(cf.get("label", ""))) or 360
        plans.append(
            {
                "mode": "combined",
                "video": cf,
                "label": f"{h}p MP4",
                "expected_bytes": c_size,
            }
        )

    if quality == "sd":
        plans.sort(key=lambda p: (0 if p["mode"] == "combined" else 1))

    return plans


async def fetch_formats(
    video_url: str,
    api_base: str,
    timeout_sec: float = 25.0,
    attempts: int = 3,
) -> dict:
    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True) as client:
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.get(
                    f"{api_base}/api/formats",
                    params={"url": video_url, "format": "json"},
                )
                if resp.status_code >= 500:
                    raise DownloadError(f"Upstream API HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("formats"):
                    return data
                raise DownloadError(data.get("error") or "No formats returned by API")
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "fetch_formats attempt %d/%d failed for %s: %s",
                    attempt,
                    attempts,
                    video_url,
                    exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(0.6 * attempt)
    raise DownloadError(f"Failed to fetch formats: {last_err}")


def _extract_error_message(raw_body: bytes, status_code: int) -> str:
    text = raw_body.decode("utf-8", errors="ignore").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("error"):
            return f"HTTP {status_code}: {parsed['error']}"
    except Exception:
        pass
    if text:
        return f"HTTP {status_code}: {text[:160]}"
    return f"HTTP {status_code}"


async def download_stream(
    video_url: str,
    fmt_id: int | str,
    dest_path: Path,
    api_base: str,
    max_bytes: int,
    timeout_sec: float = 35.0,
    attempts: int = 2,
    progress_callback: ProgressCallback = None,
) -> tuple[Path, str]:
    last_err: Optional[Exception] = None
    timeout = httpx.Timeout(connect=12.0, read=timeout_sec, write=15.0, pool=15.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(1, attempts + 1):
            try:
                async with client.stream(
                    "GET",
                    f"{api_base}/api/download",
                    params={"url": video_url, "fmt": fmt_id},
                ) as resp:
                    if resp.status_code >= 400:
                        raw_err = await resp.aread()
                        msg = _extract_error_message(raw_err, resp.status_code)
                        if resp.status_code in (403, 502) and (
                            "403" in msg or "Upstream download failed" in msg
                        ):
                            raise UpstreamRejectedError(msg)
                        raise DownloadError(msg)

                    content_len = resp.headers.get("Content-Length")
                    expected_bytes = (
                        int(content_len)
                        if content_len and content_len.isdigit()
                        else None
                    )
                    if expected_bytes and expected_bytes > max_bytes * 2:
                        raise DownloadError("Source file exceeds safety size limit")

                    header_bytes = b""
                    total = 0
                    last_report = 0.0
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with dest_path.open("wb") as fp:
                        async for chunk in resp.aiter_bytes(chunk_size=131072):
                            if not chunk:
                                continue
                            if not header_bytes:
                                header_bytes = chunk[:64]
                                trimmed = header_bytes.lstrip()
                                if (
                                    trimmed.startswith(b"{")
                                    or trimmed.startswith(b"<!DOCTYPE")
                                    or trimmed.startswith(b"<html")
                                ):
                                    raise DownloadError(
                                        "API returned non-media error payload"
                                    )
                            total += len(chunk)
                            if total > max_bytes * 2:
                                raise DownloadError(
                                    "Stream exceeded safety size limit"
                                )
                            fp.write(chunk)

                            now = time.monotonic()
                            if progress_callback and (now - last_report) >= 1.3:
                                last_report = now
                                await progress_callback(total, expected_bytes)

                    if total < 4096:
                        raise DownloadError(f"Downloaded file too small ({total} bytes)")

                    if progress_callback:
                        await progress_callback(total, expected_bytes or total)

                    ext = detect_media_extension(
                        header_bytes, resp.headers.get("Content-Type", "")
                    )
                    return dest_path, ext
            except UpstreamRejectedError as exc:
                if dest_path.exists():
                    dest_path.unlink(missing_ok=True)
                logger.warning("download_stream rejected (fmt=%s): %s", fmt_id, exc)
                raise
            except Exception as exc:
                last_err = exc
                if dest_path.exists():
                    dest_path.unlink(missing_ok=True)
                logger.warning(
                    "download_stream attempt %d/%d (fmt=%s) failed: %s",
                    attempt,
                    attempts,
                    fmt_id,
                    exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(0.5 * attempt)

    raise DownloadError(f"Stream download failed: {last_err}")


async def probe_media_info(
    ffmpeg_exe: str, media_path: Path
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe,
        "-i",
        str(media_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    text = stderr.decode("utf-8", errors="ignore")

    dur_sec: Optional[int] = None
    m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m_dur:
        dur_sec = int(
            round(
                int(m_dur.group(1)) * 3600
                + int(m_dur.group(2)) * 60
                + float(m_dur.group(3))
            )
        )

    width: Optional[int] = None
    height: Optional[int] = None
    m_res = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})\b", text)
    if m_res:
        width = int(m_res.group(1))
        height = int(m_res.group(2))

    return dur_sec, width, height


async def convert_to_mp3(
    ffmpeg_exe: str,
    input_path: Path,
    output_path: Path,
    title: str,
    artist: str,
    max_bytes: int,
) -> Path:
    for bitrate in ("192k", "128k", "96k"):
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_exe,
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            bitrate,
            "-id3v2_version",
            "3",
            "-metadata",
            f"title={title}",
            "-metadata",
            f"artist={artist}",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists():
            err_msg = stderr.decode("utf-8", errors="ignore")[-400:]
            raise DownloadError(f"ffmpeg MP3 conversion failed: {err_msg}")
        if output_path.stat().st_size <= max_bytes:
            return output_path

    raise DownloadError("Converted MP3 exceeds Telegram's 50 MB limit.")


async def mux_video_and_audio(
    ffmpeg_exe: str,
    video_path: Path,
    audio_path: Optional[Path],
    output_path: Path,
    title: str,
) -> Path:
    if audio_path is None:
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-metadata",
            f"title={title}",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if proc.returncode == 0 and output_path.exists():
            return output_path
        video_path.rename(output_path)
        return output_path

    for audio_codec in ("copy", "aac"):
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c:v",
            "copy",
            "-c:a",
            audio_codec,
            "-movflags",
            "+faststart",
            "-metadata",
            f"title={title}",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and output_path.exists():
            return output_path
        logger.warning(
            "mux_video_and_audio (%s) failed: %s",
            audio_codec,
            stderr.decode("utf-8", errors="ignore")[-250:],
        )

    raise DownloadError("Failed to merge video and audio streams.")


async def download_thumbnail(thumb_url: str, dest_path: Path) -> Optional[Path]:
    if not thumb_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(thumb_url)
            resp.raise_for_status()
            dest_path.write_bytes(resp.content)

        try:
            from PIL import Image

            with Image.open(dest_path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((320, 320))
                rgb.save(dest_path, format="JPEG", quality=85)
        except Exception:
            pass
        return dest_path
    except Exception:
        return None


def _progress_bar(pct: int) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 10)
    return f"<code>{'▰' * filled}{'▱' * (10 - filled)}</code>  <b>{pct}%</b>"


def _render_stage_msg(
    title: str,
    performer: str,
    duration: str,
    stage: str,
    pct: int,
    step: int = 0,
    mode: str = "audio",
) -> str:
    icon = "🎧" if mode == "audio" else "🎬"
    header = "Downloading Audio" if mode == "audio" else "Downloading Video"
    spinner = _SPINNERS[step % len(_SPINNERS)]
    short_title = title[:65] + ("…" if len(title) > 65 else "")

    meta_parts: list[str] = []
    if performer and performer != "Unknown Artist":
        meta_parts.append(f"<i>{html.escape(performer)}</i>")
    if duration:
        meta_parts.append(f"<code>{html.escape(duration)}</code>")
    meta_line = f"\n{'  ·  '.join(meta_parts)}" if meta_parts else ""

    return (
        f"{icon} <b>{header}</b>\n\n"
        f"<blockquote><b>{html.escape(short_title)}</b>{meta_line}</blockquote>\n"
        f"{spinner} <i>{html.escape(stage)}</i>\n"
        f"{_progress_bar(pct)}"
    )


async def download_song(
    candidates: list[SongCandidate],
    work_dir: Path,
    api_base: str,
    max_bytes: int,
    status_callback: StatusCallback = None,
) -> DownloadedTrack:
    if not candidates:
        raise DownloadError("No matching tracks found.")

    ffmpeg_exe = find_ffmpeg_exe()
    last_error: Optional[Exception] = None
    step = [0]

    def next_step() -> int:
        step[0] += 1
        return step[0]

    for idx, candidate in enumerate(candidates[:4], start=1):
        try:
            clean_title, performer = split_title_artist(
                candidate.title, candidate.channel
            )
            if status_callback:
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage="Fetching audio formats...",
                        pct=30,
                        step=next_step(),
                        mode="audio",
                    )
                )

            fmt_data = await fetch_formats(candidate.url, api_base=api_base)
            formats = fmt_data.get("formats") or []

            if (not candidate.title or candidate.title == "Audio Track") and fmt_data.get("title"):
                api_title = str(fmt_data["title"]).strip()
                if "snapyt" not in api_title.lower():
                    clean_title, performer = split_title_artist(api_title, candidate.channel)

            audio_choices = choose_audio_candidates(
                formats, has_ffmpeg=bool(ffmpeg_exe), max_bytes=max_bytes
            )
            if not audio_choices:
                raise DownloadError("No downloadable audio formats available.")

            raw_path = work_dir / f"raw_{idx}.bin"
            chosen_fmt = None
            detected_ext = ".bin"

            async def on_dl_progress(done: int, expected: Optional[int]) -> None:
                if not status_callback:
                    return
                if expected and expected > 0 and done > 0:
                    pct = 35 + int(min(1.0, done / expected) * 43)
                    size_info = f" ({done / (1024*1024):.1f} / {expected / (1024*1024):.1f} MB)"
                elif done > 0:
                    pct = 55
                    size_info = f" ({done / (1024*1024):.1f} MB)"
                else:
                    pct = 35
                    size_info = ""
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage=f"Downloading stream{size_info}...",
                        pct=pct,
                        step=next_step(),
                        mode="audio",
                    )
                )

            for fmt in audio_choices[:2]:
                try:
                    await on_dl_progress(0, parse_size_bytes(str(fmt.get("size", ""))))
                    _, detected_ext = await download_stream(
                        candidate.url,
                        fmt["fmt"],
                        raw_path,
                        api_base=api_base,
                        max_bytes=max_bytes,
                        progress_callback=on_dl_progress,
                    )
                    chosen_fmt = fmt
                    break
                except UpstreamRejectedError as exc:
                    last_error = exc
                    logger.warning(
                        "Upstream rejected candidate %d (%s) on fmt=%s; skipping to next candidate",
                        idx,
                        candidate.video_id,
                        fmt.get("fmt"),
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Format fmt=%s failed for %s: %s",
                        fmt.get("fmt"),
                        candidate.url,
                        exc,
                    )

            if not chosen_fmt or not raw_path.exists():
                raise DownloadError(f"Audio download failed: {last_error}")

            if status_callback:
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage="Converting to MP3 & tagging...",
                        pct=85,
                        step=next_step(),
                        mode="audio",
                    )
                )

            duration_sec = candidate.duration_seconds
            if ffmpeg_exe:
                if duration_sec is None:
                    duration_sec, _, _ = await probe_media_info(ffmpeg_exe, raw_path)

                if detected_ext == ".mp3" and raw_path.stat().st_size <= max_bytes:
                    final_path = work_dir / "track.mp3"
                    raw_path.rename(final_path)
                else:
                    final_path = await convert_to_mp3(
                        ffmpeg_exe,
                        raw_path,
                        work_dir / "track.mp3",
                        title=clean_title,
                        artist=performer,
                        max_bytes=max_bytes,
                    )
                    raw_path.unlink(missing_ok=True)
                final_ext = ".mp3"
            else:
                if raw_path.stat().st_size > max_bytes:
                    raise DownloadError("File exceeds Telegram's 50 MB limit.")
                final_ext = detected_ext if detected_ext != ".bin" else ".m4a"
                final_path = work_dir / f"track{final_ext}"
                raw_path.rename(final_path)

            thumb_path = await download_thumbnail(
                candidate.thumbnail_url, work_dir / "thumb.jpg"
            )
            if status_callback:
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage="Uploading to Telegram...",
                        pct=95,
                        step=next_step(),
                        mode="audio",
                    )
                )

            return DownloadedTrack(
                file_path=final_path,
                filename=sanitize_filename(clean_title, final_ext),
                title=clean_title,
                performer=performer,
                duration_seconds=duration_sec,
                thumbnail_path=thumb_path,
                format_label=str(chosen_fmt.get("label", "MP3 Audio")),
                source_url=candidate.url,
                size_bytes=final_path.stat().st_size,
                media_type="audio",
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Audio candidate %d (%s) failed: %s", idx, candidate.url, exc)
            continue

    if isinstance(last_error, DownloadError):
        raise last_error
    raise DownloadError(f"Could not download song: {last_error}")


async def download_video(
    candidates: list[SongCandidate],
    work_dir: Path,
    api_base: str,
    max_bytes: int,
    quality: str = "hd",
    status_callback: StatusCallback = None,
) -> DownloadedTrack:
    if not candidates:
        raise DownloadError("No matching videos found.")

    ffmpeg_exe = find_ffmpeg_exe()
    last_error: Optional[Exception] = None
    step = [0]

    def next_step() -> int:
        step[0] += 1
        return step[0]

    for idx, candidate in enumerate(candidates[:4], start=1):
        try:
            clean_title, performer = split_title_artist(
                candidate.title, candidate.channel
            )
            if status_callback:
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage="Fetching video formats...",
                        pct=28,
                        step=next_step(),
                        mode="video",
                    )
                )

            fmt_data = await fetch_formats(candidate.url, api_base=api_base)
            formats = fmt_data.get("formats") or []

            if (not candidate.title or candidate.title == "Audio Track") and fmt_data.get("title"):
                api_title = str(fmt_data["title"]).strip()
                if "snapyt" not in api_title.lower():
                    clean_title, performer = split_title_artist(api_title, candidate.channel)

            plans = choose_video_plans(
                formats,
                has_ffmpeg=bool(ffmpeg_exe),
                max_bytes=max_bytes,
                quality=quality,
            )
            if not plans:
                raise DownloadError("No MP4 format available under 50 MB.")

            final_path: Optional[Path] = None
            chosen_label = "MP4 Video"

            for p_idx, plan in enumerate(plans[:3]):
                v_raw = work_dir / f"v_{idx}_{p_idx}.mp4"
                a_raw = work_dir / f"a_{idx}_{p_idx}.m4a"
                out_mp4 = work_dir / f"out_{idx}_{p_idx}.mp4"
                try:
                    async def on_vid_progress(done: int, expected: Optional[int]) -> None:
                        if not status_callback:
                            return
                        exp = expected or plan.get("expected_bytes")
                        if exp and exp > 0 and done > 0:
                            pct = 35 + int(min(1.0, done / exp) * 43)
                            size_info = f" ({done / (1024*1024):.1f} / {exp / (1024*1024):.1f} MB)"
                        elif done > 0:
                            pct = 55
                            size_info = f" ({done / (1024*1024):.1f} MB)"
                        else:
                            pct = 35
                            size_info = ""
                        await status_callback(
                            _render_stage_msg(
                                title=clean_title,
                                performer=performer,
                                duration=candidate.duration_text,
                                stage=f"Downloading {plan['label']}{size_info}...",
                                pct=pct,
                                step=next_step(),
                                mode="video",
                            )
                        )

                    await on_vid_progress(0, plan.get("expected_bytes"))

                    if plan["mode"] == "mux":
                        await asyncio.gather(
                            download_stream(
                                candidate.url,
                                plan["video"]["fmt"],
                                v_raw,
                                api_base=api_base,
                                max_bytes=max_bytes,
                                progress_callback=on_vid_progress,
                            ),
                            download_stream(
                                candidate.url,
                                plan["audio"]["fmt"],
                                a_raw,
                                api_base=api_base,
                                max_bytes=max_bytes,
                            ),
                        )
                        if status_callback:
                            await status_callback(
                                _render_stage_msg(
                                    title=clean_title,
                                    performer=performer,
                                    duration=candidate.duration_text,
                                    stage="Merging video & audio...",
                                    pct=86,
                                    step=next_step(),
                                    mode="video",
                                )
                            )
                        assert ffmpeg_exe is not None
                        await mux_video_and_audio(
                            ffmpeg_exe, v_raw, a_raw, out_mp4, title=clean_title
                        )
                    else:
                        await download_stream(
                            candidate.url,
                            plan["video"]["fmt"],
                            v_raw,
                            api_base=api_base,
                            max_bytes=max_bytes,
                            progress_callback=on_vid_progress,
                        )
                        if ffmpeg_exe:
                            await mux_video_and_audio(
                                ffmpeg_exe, v_raw, None, out_mp4, title=clean_title
                            )
                        else:
                            v_raw.rename(out_mp4)

                    if out_mp4.exists() and out_mp4.stat().st_size <= max_bytes:
                        final_path = out_mp4
                        chosen_label = plan["label"]
                        break
                    raise DownloadError("Video exceeds Telegram's 50 MB limit.")
                except UpstreamRejectedError as exc:
                    last_error = exc
                    logger.warning(
                        "Upstream rejected video candidate %d (%s); skipping",
                        idx,
                        candidate.video_id,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning("Video plan %s failed: %s", plan["label"], exc)
                finally:
                    v_raw.unlink(missing_ok=True)
                    a_raw.unlink(missing_ok=True)

            if not final_path or not final_path.exists():
                raise DownloadError(f"Video download failed: {last_error}")

            dur_sec = candidate.duration_seconds
            width, height = None, None
            if ffmpeg_exe:
                probed_dur, width, height = await probe_media_info(
                    ffmpeg_exe, final_path
                )
                if dur_sec is None:
                    dur_sec = probed_dur

            thumb_path = await download_thumbnail(
                candidate.thumbnail_url, work_dir / "thumb.jpg"
            )
            if status_callback:
                await status_callback(
                    _render_stage_msg(
                        title=clean_title,
                        performer=performer,
                        duration=candidate.duration_text,
                        stage="Uploading to Telegram...",
                        pct=95,
                        step=next_step(),
                        mode="video",
                    )
                )

            return DownloadedTrack(
                file_path=final_path,
                filename=sanitize_filename(clean_title, ".mp4"),
                title=clean_title,
                performer=performer,
                duration_seconds=dur_sec,
                thumbnail_path=thumb_path,
                format_label=chosen_label,
                source_url=candidate.url,
                size_bytes=final_path.stat().st_size,
                media_type="video",
                width=width,
                height=height,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Video candidate %d (%s) failed: %s", idx, candidate.url, exc)
            continue

    if isinstance(last_error, DownloadError):
        raise last_error
    raise DownloadError(f"Could not download video: {last_error}")
