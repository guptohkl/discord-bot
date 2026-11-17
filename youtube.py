"""Everything that touches yt-dlp, plus choosing which search result to play.

Two changes from the old bot matter most:

* Searches are "flat": yt-dlp returns titles/channels/durations for the
  results without fully extracting each video. Only the one video we pick
  gets the slow full extraction, and only right before it plays (stream
  URLs expire, so resolving at queue time was what broke long queues).
* The pick is scored instead of keyword-filtered. When the track is known,
  its real duration is the strongest signal: the album audio is the upload
  whose length matches, a music video with a skit or a live cut is not.
"""
from __future__ import annotations

import asyncio
import re

import yt_dlp

import matching
from models import Candidate, Song, TrackMeta

PLAYLIST_LIMIT = 200

_BASE_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
}

# Words in a video title that mean "this is not the plain studio recording".
_BAD_WORDS = {"live", "cover", "remix", "karaoke", "instrumental", "sped", "slowed",
              "reverb", "nightcore", "8d", "clean", "censored", "edited", "bleeped", "reaction", "tutorial", "lesson",
              "mashup", "loop", "concert", "acoustic", "parody", "bass", "boosted"}
_LONG_OK = {"album", "mix", "hour", "hours", "full", "live", "set", "podcast",
            "compilation", "playlist", "radio", "concert", "session"}


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip(), re.IGNORECASE))


def _keyword_adjust(title_toks: list[str], channel: str, allowed: set[str]) -> float:
    """Bonuses/penalties shared by both scoring modes."""
    toks = set(title_toks)
    adj = 0.0
    bad = {w for w in toks & _BAD_WORDS if w not in allowed}
    adj -= min(45, 30 * len(bad))
    if {"full", "album"} <= toks and "album" not in allowed:
        adj -= 30
    is_lyric = bool(toks & {"lyric", "lyrics"})
    if channel.lower().endswith("- topic"):
        adj += 12                      # auto-generated art track = the album audio
    elif "audio" in toks:
        adj += 6
    elif is_lyric:
        adj += 3
    elif "video" in toks and ({"official", "music"} & toks):
        adj -= 8                       # music videos often have intros/skits
    return adj


def score_candidate(cand: Candidate, idx: int, *, meta: TrackMeta | None = None,
                    query: str | None = None) -> float:
    title_toks = matching.tokens(cand.title)
    channel_toks = matching.tokens(matching.clean_channel(cand.channel))

    if meta:
        core, extras = matching.split_title(meta.title)
        want_title = matching.content_tokens(core)
        want_artist = matching.content_tokens(meta.artist)
        allowed = set(want_title) | set(matching.tokens(extras)) | set(want_artist) \
            | set(matching.tokens(query or ""))
        title_cov = matching.coverage(want_title, title_toks)
        if title_cov < 0.5:
            return float("-inf")       # not this song at all
        score = 50 * title_cov + 20 * matching.coverage(want_artist, title_toks + channel_toks)
        if channel_toks and matching.coverage(channel_toks, want_artist) == 1.0 \
                and matching.coverage(want_artist, channel_toks) == 1.0:
            score += 5                 # the artist's own channel over a re-upload
        if meta.duration and cand.duration:
            diff = abs(cand.duration - meta.duration)
            score += 30 if diff <= 2 else 24 if diff <= 5 else 12 if diff <= 15 \
                else 0 if diff <= 40 else -25
            if cand.duration > 2 * meta.duration + 60:
                score -= 50            # full album / 1 hour loop
        score += _keyword_adjust(title_toks, cand.channel, allowed)
        return score - 1.5 * idx

    # No metadata: trust YouTube's ordering more, just steer away from junk.
    q = matching.content_tokens(query or "")
    allowed = set(matching.tokens(query or ""))
    score = 40 * matching.coverage(q, title_toks + channel_toks)
    score += _keyword_adjust(title_toks, cand.channel, allowed)
    if cand.duration and cand.duration > 20 * 60 and not (allowed & _LONG_OK):
        score -= 15
    return score - 3.0 * idx


def pick_best(candidates: list[Candidate], *, meta: TrackMeta | None = None,
              query: str | None = None) -> Candidate | None:
    """Best search result for a known track (`meta`) or a raw `query`.

    With `meta`, returns None when no result contains the song's title, so
    callers can tell "found it" from "YouTube gave us something unrelated".
    """
    scored = [(score_candidate(c, i, meta=meta, query=query), -i, c)
              for i, c in enumerate(candidates)]
    scored = [s for s in scored if s[0] != float("-inf")]
    if not scored:
        return None
    return max(scored, key=lambda s: (s[0], s[1]))[2]


class YouTube:
    """Async wrapper around yt-dlp. `extract` is injectable for tests."""

    def __init__(self, extract=None):
        self._extract = extract or self._ytdlp_extract

    @staticmethod
    def _ytdlp_extract(query: str, flat: bool) -> dict:
        opts = dict(_BASE_OPTS)
        if flat:
            # Flatten playlists/searches only; a single video URL still gets
            # a full extraction (stream URL, track/artist fields).
            opts.update(extract_flat="in_playlist", playlistend=PLAYLIST_LIMIT)
        else:
            opts.update(extract_flat=False, noplaylist=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.sanitize_info(ydl.extract_info(query, download=False))

    async def search(self, query: str, limit: int = 6) -> list[Candidate]:
        info = await asyncio.to_thread(self._extract, f"ytsearch{limit}:{query}", True)
        return [Candidate.from_entry(e) for e in (info or {}).get("entries") or [] if e]

    async def fetch_url(self, url: str) -> tuple[str, dict]:
        """Returns ("playlist", info with flat entries) or ("video", full info)."""
        info = await asyncio.to_thread(self._extract, url.strip(), True)
        if info.get("_type") == "playlist" or "entries" in info:
            info["entries"] = [e for e in info.get("entries") or []
                               if e and e.get("title") not in ("[Private video]", "[Deleted video]")]
            return "playlist", info
        return "video", info

    async def resolve(self, song: Song) -> dict:
        """Full extraction of one video: fills in the (expiring) stream URL."""
        info = await asyncio.to_thread(self._extract, song.url, False)
        if not info or not info.get("url"):
            raise RuntimeError(f"No playable stream for {song.url}")
        song.apply_info(info)
        return info

    async def find(self, meta: TrackMeta, query: str | None = None) -> Candidate | None:
        """Search YouTube for a known track and pick the matching upload."""
        candidates = await self.search(query or f"{meta.artist} - {meta.title}")
        return pick_best(candidates, meta=meta)
