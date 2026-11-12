from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx

_YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_ID_PATTERNS = [
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|/live/)([0-9A-Za-z_-]{11})"),
    re.compile(r"youtu\.be/([0-9A-Za-z_-]{11})"),
]


@dataclass
class SongCandidate:
    video_id: str
    url: str
    title: str
    channel: str = ""
    duration_text: str = ""
    duration_seconds: Optional[int] = None
    thumbnail_url: str = ""


def is_url(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("http://") or stripped.startswith("https://")


def extract_youtube_id(url: str) -> Optional[str]:
    for pat in _YT_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def parse_duration_seconds(duration_text: str) -> Optional[int]:
    if not duration_text:
        return None
    parts = duration_text.strip().split(":")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def split_title_artist(raw_title: str, channel: str = "") -> tuple[str, str]:
    title = raw_title.strip()
    performer = re.sub(r"\s*-\s*Topic$", "", channel.strip(), flags=re.IGNORECASE)
    if " - " in title and performer:
        left, right = title.split(" - ", 1)
        left_clean = left.strip()
        right_clean = right.strip()
        perf_norm = re.sub(r"(vevo|official|music)$", "", performer.lower()).strip()
        left_norm = left_clean.lower()
        if perf_norm and (perf_norm in left_norm or left_norm in perf_norm) and right_clean:
            return right_clean, left_clean
    return title, performer or "Unknown Artist"


def _extract_yt_initial_data(html: str) -> dict:
    patterns = [
        re.compile(r"var\s+ytInitialData\s*=\s*(\{.*?\});\s*</script>", re.DOTALL),
        re.compile(r"window\[[\"']ytInitialData[\"']\]\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"ytInitialData\s*=\s*(\{.*?\});", re.DOTALL),
    ]
    for pat in patterns:
        m = pat.search(html)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    raise ValueError("Could not parse YouTube search results.")


def _collect_video_renderers(node: object, out: list[dict]) -> None:
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            out.append(node["videoRenderer"])
            return
        for v in node.values():
            _collect_video_renderers(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_video_renderers(item, out)


async def search_youtube(
    query: str,
    limit: int = 5,
    max_duration_seconds: int = 2400,
) -> list[SongCandidate]:
    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(
        query.strip()
    )
    async with httpx.AsyncClient(
        headers=_YT_HEADERS, timeout=20.0, follow_redirects=True
    ) as client:
        resp = await client.get(search_url)
        resp.raise_for_status()

    data = _extract_yt_initial_data(resp.text)
    renderers: list[dict] = []
    _collect_video_renderers(data, renderers)

    candidates: list[SongCandidate] = []
    fallback_long: list[SongCandidate] = []
    seen_ids: set[str] = set()

    for vr in renderers:
        vid = vr.get("videoId")
        if not vid or vid in seen_ids:
            continue

        title = "".join(
            run.get("text", "") for run in vr.get("title", {}).get("runs", [])
        ).strip()
        if not title:
            continue

        dur_text = vr.get("lengthText", {}).get("simpleText", "").strip()
        if not dur_text:
            # Skip live streams / premieres without fixed duration
            continue

        dur_sec = parse_duration_seconds(dur_text)
        channel = "".join(
            run.get("text", "") for run in vr.get("ownerText", {}).get("runs", [])
        ).strip()
        thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
        thumb_url = thumbs[-1].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

        candidate = SongCandidate(
            video_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            title=title,
            channel=channel,
            duration_text=dur_text,
            duration_seconds=dur_sec,
            thumbnail_url=thumb_url,
        )
        seen_ids.add(vid)

        if dur_sec is not None and dur_sec > max_duration_seconds:
            fallback_long.append(candidate)
            continue

        candidates.append(candidate)
        if len(candidates) >= limit:
            break

    if not candidates and fallback_long:
        return fallback_long[:limit]
    return candidates


async def get_url_metadata(url: str) -> SongCandidate:
    vid = extract_youtube_id(url) or ""
    clean_url = f"https://www.youtube.com/watch?v={vid}" if vid else url.strip()

    if vid:
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get(
                    "https://www.youtube.com/oembed",
                    params={"url": clean_url, "format": "json"},
                )
                if r.status_code == 200:
                    info = r.json()
                    return SongCandidate(
                        video_id=vid,
                        url=clean_url,
                        title=info.get("title") or "Song",
                        channel=info.get("author_name") or "",
                        thumbnail_url=info.get("thumbnail_url")
                        or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    )
        except Exception:
            pass

    return SongCandidate(
        video_id=vid,
        url=clean_url,
        title="Audio Track",
        channel="",
        thumbnail_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "",
    )
