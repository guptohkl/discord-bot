"""Plain data classes shared by every module. No network, no Discord."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# YouTube stream URLs die after a few hours, so anything older than this is
# re-fetched right before playback.
STREAM_MAX_AGE = 3 * 60 * 60


@dataclass
class TrackMeta:
    """What a song *is* (as opposed to which YouTube video we play for it)."""
    artist: str
    title: str
    album: str | None = None
    duration: int | None = None      # seconds
    source: str = ""                 # deezer / itunes / lastfm / youtube
    popularity: int | None = None
    explicit: bool | None = None

    def __str__(self):
        return f"{self.artist} - {self.title}"


@dataclass
class AlbumRef:
    """An album search hit, before its tracklist has been fetched."""
    id: str
    artist: str
    title: str
    track_count: int | None = None
    record_type: str = "album"       # album / ep / single
    source: str = ""


@dataclass
class AlbumMeta:
    artist: str
    title: str
    tracks: list[TrackMeta]
    source: str = ""

    @property
    def duration(self):
        return sum(t.duration or 0 for t in self.tracks)


@dataclass
class Candidate:
    """One YouTube search result / playlist entry (cheap 'flat' data)."""
    url: str
    title: str
    channel: str = ""
    duration: int | None = None

    @classmethod
    def from_entry(cls, entry: dict) -> "Candidate":
        url = entry.get("webpage_url") or entry.get("url")
        if not url or not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={entry.get('id')}"
        duration = entry.get("duration")
        return cls(
            url=url,
            title=entry.get("title") or "Unknown",
            channel=entry.get("channel") or entry.get("uploader") or "",
            duration=int(duration) if duration else None,
        )


@dataclass
class Song:
    """Something sitting in a queue.

    A Song can exist before we know which YouTube video it is (album tracks
    and autoplay picks start out as just metadata + a search string). The
    player fills in `url` and `stream_url` right before it is needed.
    """
    title: str                        # display title (YouTube title once known)
    url: str | None = None            # YouTube watch URL, None until resolved
    duration: int | None = None
    meta: TrackMeta | None = None     # identified artist/track, if any
    requester: str | None = None
    search_query: str | None = None   # used to find `url` lazily
    stream_url: str | None = None
    stream_fetched_at: float = 0.0
    source: str = "youtube"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_candidate(cls, cand: Candidate, meta: TrackMeta | None = None) -> "Song":
        return cls(title=cand.title, url=cand.url, duration=cand.duration, meta=meta)

    @classmethod
    def from_meta(cls, meta: TrackMeta) -> "Song":
        return cls(title=str(meta), duration=meta.duration, meta=meta,
                   search_query=f"{meta.artist} - {meta.title}")

    @classmethod
    def from_info(cls, info: dict, meta: TrackMeta | None = None) -> "Song":
        """Build from a full (non-flat) yt-dlp result, stream URL included."""
        song = cls(title=info.get("title") or "Unknown",
                   url=info.get("webpage_url") or info.get("original_url"),
                   meta=meta)
        song.apply_info(info)
        return song

    # -- helpers ------------------------------------------------------------
    def apply_info(self, info: dict):
        if info.get("url"):
            self.stream_url = info["url"]
            self.stream_fetched_at = time.time()
        if info.get("duration"):
            self.duration = int(info["duration"])
        if info.get("title"):
            self.title = info["title"]
        if info.get("webpage_url"):
            self.url = info["webpage_url"]

    def stream_is_fresh(self) -> bool:
        return bool(self.stream_url) and (time.time() - self.stream_fetched_at) < STREAM_MAX_AGE

    # Old attribute names, kept so nothing that used them breaks.
    @property
    def track(self):
        return self.meta.title if self.meta else None

    @property
    def artist(self):
        return self.meta.artist if self.meta else None

    @property
    def audio_url(self):
        return self.stream_url

    def __str__(self):
        return f"{self.title} ({self.source})"
