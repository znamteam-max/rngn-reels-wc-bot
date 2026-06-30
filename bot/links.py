from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bot.config import get_settings


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igsh",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "si",
}


@dataclass(frozen=True)
class LinkInfo:
    url: str
    external_id: str | None = None


def is_skip_text(text: str | None) -> bool:
    if not text:
        return False
    return text.strip().lower() in {
        "-",
        "skip",
        "пропустить",
        "нет",
        "no",
        "none",
    }


def _ensure_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("empty URL")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value
    return value


def _clean_query(query: str, keep: set[str] | None = None) -> str:
    keep = keep or set()
    parsed = parse_qs(query, keep_blank_values=False)
    cleaned: dict[str, list[str]] = {}
    for key, values in parsed.items():
        lower_key = key.lower()
        if lower_key in keep:
            cleaned[key] = values
            continue
        if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
            continue
        cleaned[key] = values
    return urlencode(cleaned, doseq=True)


def clean_url(raw: str, keep_query: set[str] | None = None) -> str:
    parsed = urlparse(_ensure_url(raw))
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path)
    query = _clean_query(parsed.query, keep_query)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalize_instagram(raw: str) -> LinkInfo:
    parsed = urlparse(_ensure_url(raw))
    host = parsed.netloc.lower()
    if "instagram.com" not in host:
        raise ValueError("Нужна ссылка Instagram/Reels.")

    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("reel", "p", "tv"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                shortcode = parts[index + 1]
                return LinkInfo(
                    url=f"https://www.instagram.com/{marker}/{shortcode}/",
                    external_id=shortcode,
                )
    raise ValueError("Не удалось найти shortcode Instagram в ссылке.")


def normalize_youtube(raw: str) -> LinkInfo:
    parsed = urlparse(_ensure_url(raw))
    host = parsed.netloc.lower().replace("www.", "")
    parts = [part for part in parsed.path.split("/") if part]
    video_id: str | None = None

    if host == "youtu.be" and parts:
        video_id = parts[0]
    elif host.endswith("youtube.com"):
        query = parse_qs(parsed.query)
        if parsed.path == "/watch" and query.get("v"):
            video_id = query["v"][0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed"}:
            video_id = parts[1]

    if video_id:
        return LinkInfo(url=f"https://youtu.be/{video_id}", external_id=video_id)
    return LinkInfo(url=clean_url(raw))


def normalize_tiktok(raw: str) -> LinkInfo:
    cleaned = clean_url(raw)
    match = re.search(r"/video/(\d+)", urlparse(cleaned).path)
    return LinkInfo(url=cleaned, external_id=match.group(1) if match else None)


def normalize_vk(raw: str) -> LinkInfo:
    cleaned = clean_url(raw)
    parsed = urlparse(cleaned)
    haystack = parsed.path
    if parsed.query:
        haystack += "?" + parsed.query
    match = re.search(r"(clip-?\d+_\d+|video-?\d+_\d+)", haystack)
    return LinkInfo(url=cleaned, external_id=match.group(1) if match else None)


def normalize_optional(platform: str, raw: str) -> LinkInfo | None:
    if is_skip_text(raw):
        return None
    if platform == "youtube":
        return normalize_youtube(raw)
    if platform == "tiktok":
        return normalize_tiktok(raw)
    if platform == "vk":
        return normalize_vk(raw)
    raise ValueError(f"Unknown platform: {platform}")


def parse_publish_date(raw: str) -> date:
    value = raw.strip().lower()
    today = datetime.now(get_settings().tz).date()
    if value in {"сегодня", "today"}:
        return today
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").date()
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", value):
        day, month = (int(part) for part in value.split("."))
        return date(today.year, month, day)
    raise ValueError("Введите дату в формате YYYY-MM-DD или DD.MM.")

