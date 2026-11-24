"""Trailer sources: YouTube channel RSS feeds and TMDB.

Only uses the standard library plus an aiohttp session passed in by the bot,
so the parsing logic can be tested without Discord installed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("trailerbot.sources")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
_CHANNEL_ID_PATTERNS = [
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[\w-]{22})"'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
]
TMDB_API = "https://api.themoviedb.org/3"


@dataclass
class Trailer:
    video_id: str
    title: str
    url: str
    source_key: str          # e.g. "yt:UCxxxx" or "tmdb" - used for first-run seeding
    source_name: str         # human readable, e.g. "Warner Bros. Pictures"
    published: datetime | None = None
    thumbnail: str | None = None
    extra: dict = field(default_factory=dict)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_trailer(title: str, include: list[str], exclude: list[str]) -> bool:
    t = title.lower()
    if any(word.lower() in t for word in exclude):
        return False
    return not include or any(word.lower() in t for word in include)


# --------------------------------------------------------------------------- YouTube

def parse_youtube_feed(xml_text: str, channel_id: str) -> list[Trailer]:
    root = ET.fromstring(xml_text)
    channel_name = root.findtext("atom:author/atom:name", default=None, namespaces=NS) \
        or root.findtext("atom:title", default=channel_id, namespaces=NS)
    items: list[Trailer] = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default=None, namespaces=NS)
        if not vid:
            continue
        link = entry.find("atom:link[@rel='alternate']", NS)
        url = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        thumb = entry.find("media:group/media:thumbnail", NS)
        items.append(Trailer(
            video_id=vid,
            title=(entry.findtext("atom:title", default="", namespaces=NS) or "").strip(),
            url=url,
            source_key=f"yt:{channel_id}",
            source_name=channel_name,
            published=parse_iso(entry.findtext("atom:published", default=None, namespaces=NS)),
            thumbnail=thumb.get("url") if thumb is not None else None,
        ))
    return items


def extract_channel_id(html: str) -> str | None:
    for pattern in _CHANNEL_ID_PATTERNS:
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def normalize_channel_ref(ref: str) -> tuple[str | None, str | None]:
    """Return (channel_id, handle) - exactly one is set."""
    ref = ref.strip()
    if CHANNEL_ID_RE.match(ref):
        return ref, None
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", ref)
    if m:
        return m.group(1), None
    m = re.search(r"youtube\.com/(@[\w.\-]+)", ref)
    if m:
        return None, m.group(1)
    return None, ref if ref.startswith("@") else "@" + ref


async def resolve_channel(session, ref: str, cache: dict) -> str | None:
    channel_id, handle = normalize_channel_ref(ref)
    if channel_id:
        return channel_id
    key = handle.lower()
    if key in cache:
        return cache[key]
    async with session.get(f"https://www.youtube.com/{handle}") as resp:
        if resp.status != 200:
            log.warning("Could not look up YouTube handle %s (HTTP %s)", handle, resp.status)
            return None
        html = await resp.text()
    channel_id = extract_channel_id(html)
    if channel_id:
        cache[key] = channel_id
        log.info("Resolved %s -> %s", handle, channel_id)
    else:
        log.warning("No channel ID found on page for %s - check the handle in config.json", handle)
    return channel_id


async def fetch_youtube(session, refs: list[str], cache: dict, include, exclude,
                        skip_shorts: bool) -> list[Trailer]:
    results: list[Trailer] = []
    for ref in refs:
        try:
            channel_id = await resolve_channel(session, ref, cache)
            if not channel_id:
                continue
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("Feed for %s returned HTTP %s", ref, resp.status)
                    continue
                xml_text = await resp.text()
            for item in parse_youtube_feed(xml_text, channel_id):
                if skip_shorts and "/shorts/" in item.url:
                    continue
                if is_trailer(item.title, include, exclude):
                    results.append(item)
        except Exception as exc:  # one bad channel shouldn't stop the rest
            log.warning("YouTube source %s failed: %s", ref, exc)
    return results


# --------------------------------------------------------------------------- TMDB

def _tmdb_auth(api_key: str) -> tuple[dict, dict]:
    """TMDB accepts a v3 API key (query param) or a v4 read token (Bearer)."""
    if api_key.startswith("eyJ"):
        return {"Authorization": f"Bearer {api_key}"}, {}
    return {}, {"api_key": api_key}


def parse_tmdb_videos(movie: dict, videos: list[dict], official_only: bool) -> list[Trailer]:
    items = []
    for v in videos:
        if v.get("site") != "YouTube" or v.get("type") not in ("Trailer", "Teaser"):
            continue
        if official_only and not v.get("official", False):
            continue
        key = v.get("key")
        if not key:
            continue
        name = (v.get("name") or v.get("type")).strip()
        items.append(Trailer(
            video_id=key,
            title=f"{movie.get('title', 'Unknown movie')} - {name}",
            url=f"https://www.youtube.com/watch?v={key}",
            source_key="tmdb",
            source_name="TMDB",
            published=parse_iso(v.get("published_at")),
            extra={"release_date": movie.get("release_date"), "tmdb_id": movie.get("id")},
        ))
    return items


async def fetch_tmdb(session, api_key: str, cfg: dict) -> list[Trailer]:
    headers, base_params = _tmdb_auth(api_key)
    region = cfg.get("region", "US")
    movies: dict[int, dict] = {}

    async def get_json(path: str, **params):
        async with session.get(f"{TMDB_API}{path}", headers=headers,
                               params={**base_params, **params}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TMDB {path} returned HTTP {resp.status}")
            return await resp.json()

    for list_name in cfg.get("lists", ["upcoming", "now_playing"]):
        for page in range(1, int(cfg.get("pages", 2)) + 1):
            try:
                data = await get_json(f"/movie/{list_name}", page=page,
                                      language="en-US", region=region)
            except Exception as exc:
                log.warning("TMDB list %s page %s failed: %s", list_name, page, exc)
                break
            for m in data.get("results", []):
                movies[m["id"]] = m
            if page >= data.get("total_pages", 1):
                break

    sem = asyncio.Semaphore(5)

    async def videos_for(movie):
        async with sem:
            try:
                data = await get_json(f"/movie/{movie['id']}/videos", language="en-US")
            except Exception as exc:
                log.debug("TMDB videos for %s failed: %s", movie.get("title"), exc)
                return []
            return parse_tmdb_videos(movie, data.get("results", []),
                                     cfg.get("official_only", True))

    batches = await asyncio.gather(*(videos_for(m) for m in movies.values()))
    return [t for batch in batches for t in batch]
