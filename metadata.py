"""Song / album recognition.

Replaces MusicBrainz. MusicBrainz needs to be told which words are the
artist and which are the title; Deezer and iTunes both take one free-text
box ("creep radiohead", "kendrick humble") and return popularity-ranked
tracks with durations, which is exactly the shape a Discord command has.

Deezer is tried first (no key, generous rate limit, has album tracklists),
iTunes is the fallback (no key either) if Deezer is down or finds nothing.
For albums only, MusicBrainz is a third fallback: it has the free mixtapes
the two streaming catalogs lack.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import Counter
from itertools import zip_longest

import aiohttp

import matching
from models import AlbumMeta, AlbumRef, TrackMeta

MAX_SONG_SECONDS = 15 * 60     # longer videos are mixes/albums/podcasts, not "a song"

ALBUM_CONTENDERS = 4           # same-titled albums compared when no artist is typed
SEARCH_LIMIT = 25              # results requested per search


class Http:
    """One shared aiohttp session. Tests swap this for a fake."""

    def __init__(self):
        self._session = None

    async def get_json(self, url, params=None, timeout=8, headers=None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": "DiscordMusicBot/2.0"})
        async with self._session.get(url, params=params, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)   # iTunes sends text/javascript

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class ProviderError(Exception):
    pass


class Deezer:
    name = "deezer"
    has_popularity = True
    BASE = "https://api.deezer.com"

    def __init__(self, http):
        self.http = http

    async def _get(self, path, **params):
        data = await self.http.get_json(f"{self.BASE}{path}", params)
        if isinstance(data, dict) and data.get("error"):
            raise ProviderError(f"Deezer error: {data['error']}")
        return data.get("data", []) if isinstance(data, dict) else []

    @staticmethod
    def _track(d, album=None):
        return TrackMeta(
            artist=(d.get("artist") or {}).get("name", ""),
            title=d.get("title", ""),
            album=album or (d.get("album") or {}).get("title"),
            duration=d.get("duration") or None,
            source="deezer",
            popularity=d.get("rank"),
            explicit=d.get("explicit_lyrics"),
        )

    async def search_tracks(self, query, limit=SEARCH_LIMIT):
        rows = await self._get("/search", q=query, limit=limit)
        return [self._track(d) for d in rows if d.get("title") and d.get("artist")]

    async def search_albums(self, query, limit=SEARCH_LIMIT):
        rows = await self._get("/search/album", q=query, limit=limit)
        return [AlbumRef(id=str(d["id"]), artist=(d.get("artist") or {}).get("name", ""),
                         title=d.get("title", ""), track_count=d.get("nb_tracks"),
                         record_type=d.get("record_type") or "album", source="deezer")
                for d in rows if d.get("id") and d.get("title")]

    async def album_tracks(self, ref):
        rows = await self._get(f"/album/{ref.id}/tracks", limit=200)
        rows.sort(key=lambda d: (d.get("disk_number") or 1, d.get("track_position") or 0))
        return [self._track(d, album=ref.title) for d in rows if d.get("title")]


class ITunes:
    name = "itunes"
    has_popularity = False
    BASE = "https://itunes.apple.com"

    def __init__(self, http):
        self.http = http

    @staticmethod
    def _track(d):
        ms = d.get("trackTimeMillis")
        return TrackMeta(
            artist=d.get("artistName", ""), title=d.get("trackName", ""),
            album=d.get("collectionName"), duration=round(ms / 1000) if ms else None,
            source="itunes", explicit=d.get("trackExplicitness") == "explicit",
        )

    async def search_tracks(self, query, limit=SEARCH_LIMIT):
        data = await self.http.get_json(f"{self.BASE}/search", {
            "term": query, "media": "music", "entity": "song", "limit": limit})
        return [self._track(d) for d in data.get("results", [])
                if d.get("trackName") and d.get("artistName")]

    async def search_albums(self, query, limit=SEARCH_LIMIT):
        data = await self.http.get_json(f"{self.BASE}/search", {
            "term": query, "media": "music", "entity": "album", "limit": limit})
        refs = []
        for d in data.get("results", []):
            title, record_type = d.get("collectionName", ""), "album"
            # iTunes marks short releases in the name itself.
            for suffix, kind in ((" - Single", "single"), (" - EP", "ep")):
                if title.endswith(suffix):
                    title, record_type = title[: -len(suffix)], kind
            if d.get("collectionId") and title:
                refs.append(AlbumRef(id=str(d["collectionId"]), artist=d.get("artistName", ""),
                                     title=title, track_count=d.get("trackCount"),
                                     record_type=record_type, source="itunes"))
        return refs

    async def album_tracks(self, ref):
        data = await self.http.get_json(f"{self.BASE}/lookup", {
            "id": ref.id, "entity": "song", "limit": 200})
        rows = [d for d in data.get("results", []) if d.get("wrapperType") == "track"]
        rows.sort(key=lambda d: (d.get("discNumber") or 1, d.get("trackNumber") or 0))
        return [self._track(d) for d in rows]


class MusicBrainz:
    """Album tracklists for releases the streaming catalogs do not carry.

    Free mixtapes (uncleared samples, never sold) are missing from Deezer
    and iTunes, but MusicBrainz is community-maintained and has them, with
    track lengths. It is only used for albums, and only after the other two
    have missed: its search is slower, it allows one request per second,
    and it has no popularity data.
    """
    name = "musicbrainz"
    has_popularity = False
    BASE = "https://musicbrainz.org/ws/2"
    MIN_INTERVAL = 1.1          # seconds between requests (their limit is 1/sec)
    # MusicBrainz sheds load with random 503s when the whole service is busy
    # (nothing to do with this bot's own rate). Waiting and retrying works.
    RETRY_DELAYS = (1, 2, 4)

    def __init__(self, http):
        self.http = http
        self._lock = asyncio.Lock()
        self._last = 0.0
        # MusicBrainz asks for an identifying User-Agent with a contact.
        self._headers = {"User-Agent": "{}/{} ( {} )".format(
            os.getenv("MUSICBRAINZ_APP_NAME", "DiscordMusicBot"),
            os.getenv("MUSICBRAINZ_VERSION", "2.0"),
            os.getenv("MUSICBRAINZ_CONTACT", "noreply@example.com"))}

    async def _get(self, path, **params):
        async with self._lock:
            for attempt, delay in enumerate((*self.RETRY_DELAYS, None), 1):
                wait = self._last + self.MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    return await self.http.get_json(f"{self.BASE}/{path}", {**params, "fmt": "json"},
                                                    timeout=12, headers=self._headers)
                except Exception as e:
                    busy = getattr(e, "status", None) in (429, 502, 503, 504) \
                        or isinstance(e, (asyncio.TimeoutError, aiohttp.ClientConnectionError))
                    if delay is None or not busy:
                        raise
                    print(f"[musicbrainz] busy ({e.__class__.__name__} "
                          f"{getattr(e, 'status', '')}), retry {attempt} in {delay}s")
                    await asyncio.sleep(delay)
                finally:
                    self._last = time.monotonic()

    @staticmethod
    def _credit(credits, default=""):
        return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits or []) or default

    async def search_tracks(self, query, limit=SEARCH_LIMIT):
        return []               # albums only; single tracks fall through to YouTube

    async def search_albums(self, query, limit=10):
        # The query goes to a Lucene parser, so strip its operator characters.
        text = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", query)
        data = await self._get("release-group", query=text, limit=limit)
        refs = []
        for d in data.get("release-groups", []):
            kind = (d.get("primary-type") or "other").lower()
            if "Compilation" in (d.get("secondary-types") or []):
                kind = "compilation"
            if d.get("id") and d.get("title"):
                refs.append(AlbumRef(id=d["id"], artist=self._credit(d.get("artist-credit")),
                                     title=d["title"], record_type=kind, source="musicbrainz"))
        return refs

    async def album_tracks(self, ref):
        data = await self._get("release", **{"release-group": ref.id, "limit": 10,
                                             "inc": "recordings artist-credits"})
        releases = [r for r in data.get("releases", []) if any(m.get("tracks") for m in r.get("media", []))]
        if not releases:
            return []
        # A release group holds several pressings (bonus tracks, promos).
        # Take the most common track count: that is the standard edition.
        count = lambda r: sum(len(m.get("tracks", [])) for m in r.get("media", []))
        usual = Counter(count(r) for r in releases).most_common(1)[0][0]
        release = next(r for r in releases if count(r) == usual)
        tracks = []
        for medium in release.get("media", []):
            for t in medium.get("tracks", []):
                length = t.get("length") or (t.get("recording") or {}).get("length")
                tracks.append(TrackMeta(artist=self._credit(t.get("artist-credit"), ref.artist),
                                        title=t.get("title", ""), album=ref.title,
                                        duration=round(length / 1000) if length else None,
                                        source="musicbrainz"))
        return [t for t in tracks if t.title]


def _track_adjust(t: TrackMeta, ms, idx) -> float:
    """Tie-breakers: provider order, then popular and explicit (not the clean edit)."""
    pop = min((t.popularity or 0) / 1_000_000, 1.0)
    return matching.position_penalty(idx) + 4.0 * pop + (1.0 if t.explicit else 0.0)


def _track_accept(ms, t: TrackMeta) -> bool:
    # A title with no artist ("mr brightside") is not settled from the catalog:
    # every recording of that title ties, and live testing showed the catalog's
    # order puts covers first. Those queries go to YouTube, whose top result is
    # reliably the famous version, and are identified from that video instead.
    return ms.artist_cov > 0


def _album_adjust(ref: AlbumRef, ms, idx) -> float:
    kind = {"album": 0.0, "ep": -4.0, "compilation": -6.0, "single": -12.0}.get(ref.record_type, -4.0)
    # With no artist typed the order is not trusted (see identify_album).
    return kind + (0.0 if ms.artist_cov == 0 else matching.position_penalty(idx))


class Identifier:
    """Turns messy input (typed text, a YouTube video) into TrackMeta/AlbumMeta."""

    def __init__(self, providers):
        self.providers = list(providers)

    # -- typed text -----------------------------------------------------------
    async def _search(self, provider, search, query, adjust, accept=None):
        """Search a provider; returns acceptable matches for `query`, best first.

        These APIs want every word to hit, so one typo ("kendrik") or one
        stray word returns nothing. If the straight search has no match, the
        primary provider is retried with each word left out in turn; the
        scoring still runs against the full query (typo-tolerantly), so a
        wrong song cannot sneak in this way.
        """
        text = matching.search_text(query)
        found = matching.shortlist(query, await search(text), adjust=adjust, accept=accept)
        words = text.split()
        if found or provider is not self.providers[0] or not 3 <= len(words) <= 7:
            return found
        variants = [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
        results = await asyncio.gather(*(search(v) for v in variants), return_exceptions=True)
        lists = [r for r in results if isinstance(r, list)]
        merged, seen = [], set()
        for row in zip_longest(*lists):               # interleave: each list's top hits first
            for item in row:
                key = (item.artist, item.title, getattr(item, "id", None)) if item else None
                if item and key not in seen:
                    seen.add(key)
                    merged.append(item)
        return matching.shortlist(query, merged, adjust=adjust, accept=accept)

    async def identify_track(self, query: str) -> TrackMeta | None:
        for provider in self.providers:
            try:
                found = await self._search(provider, provider.search_tracks, query,
                                           _track_adjust, _track_accept)
            except Exception as e:
                print(f"[identify] {provider.name} failed: {e!r}")
                continue
            if found:
                best = found[0][2]
                print(f'[identify] "{query}" -> {best} ({provider.name})')
                return best
        print(f'[identify] no match for "{query}"')
        return None

    async def identify_album(self, query: str) -> AlbumMeta | None:
        # First choice: a provider with an album whose WHOLE title was typed.
        # "action bronson blue chips" must not settle for Deezer's "Blue Chips
        # 7000" when a later provider has "Blue Chips" itself. Partial-title
        # matches ("good kid" for "good kid, m.A.A.d city") are kept as a
        # second choice, used only if no provider has a full one.
        partial = []
        for provider in self.providers:
            try:
                found = await self._search(provider, provider.search_albums, query, _album_adjust)
            except Exception as e:
                print(f"[identify] {provider.name} album search failed: {e!r}")
                continue
            full = [r for r in found if r[1].title_cov >= 0.99]
            if full:
                album = await self._load_album(provider, full, query)
                if album:
                    return album
            elif found:
                partial.append((provider, found))
        for provider, found in partial:
            album = await self._load_album(provider, found, query)
            if album:
                return album
        print(f'[identify] no album match for "{query}"')
        return None

    async def _load_album(self, provider, found, query) -> AlbumMeta | None:
        try:
            ref, tracks = await self._pick_album(provider, found)
        except Exception as e:
            print(f"[identify] {provider.name} album lookup failed: {e!r}")
            return None
        if not tracks:
            return None
        print(f'[identify] album "{query}" -> {ref.artist} - {ref.title} '
              f'({len(tracks)} tracks, {provider.name})')
        return AlbumMeta(artist=ref.artist, title=ref.title, tracks=tracks, source=provider.name)

    async def _pick_album(self, provider, found):
        """Choose between album matches; returns (ref, tracks).

        If an artist was typed, the top match stands. If only a title was
        typed ("dark side of the moon"), several artists' albums tie and
        album search results carry no popularity, so the tracklists of the
        closest few are fetched and the album with the best known tracks wins.
        """
        top_score, top_ms, top = found[0]
        contenders = [r for r in found[:ALBUM_CONTENDERS] if top_score - r[0] <= 6]
        if top_ms.artist_cov > 0 or len(contenders) < 2 or not getattr(provider, "has_popularity", True):
            return top, await provider.album_tracks(top)
        tracklists = await asyncio.gather(*(provider.album_tracks(r[2]) for r in contenders),
                                          return_exceptions=True)
        best, best_tracks, best_value = top, None, float("-inf")
        for (score, _, ref), tracks in zip(contenders, tracklists):
            if not isinstance(tracks, list) or not tracks:
                continue
            pops = [t.popularity for t in tracks if t.popularity is not None]
            mean_pop = sum(pops) / len(pops) / 1_000_000 if pops else 0.0
            value = score + 12.0 * min(mean_pop, 1.0)
            if value > best_value:
                best, best_tracks, best_value = ref, tracks, value
        if best_tracks is None:
            return top, await provider.album_tracks(top)
        return best, best_tracks

    # -- YouTube videos ---------------------------------------------------------
    async def verify(self, text: str, *, duration=None, min_query_cov=0.7) -> TrackMeta | None:
        """Strict check that `text` (from a video title) names a real track.

        Much stricter than identify_track: the candidate's whole artist AND
        whole title must be in the text, most of the text must be explained,
        and the lengths must be compatible. This is what stops a random
        YouTube video from seeding autoplay with a wrong song.
        """
        if not matching.normalize(text):
            return None
        for provider in self.providers:
            try:
                candidates = await provider.search_tracks(matching.search_text(text), limit=8)
            except Exception as e:
                print(f"[identify] {provider.name} failed: {e!r}")
                continue
            for final, ms, cand in matching.rank(text, candidates, adjust=_track_adjust):
                if (ms.strict and ms.query_cov >= min_query_cov
                        and matching.duration_plausible(duration, cand.duration)):
                    return cand
        return None

    async def identify_video(self, info: dict) -> TrackMeta | None:
        """Work out which song a YouTube video is, or None if it is not one.

        `info` is a yt-dlp result: a full one for single videos, or a flat
        playlist entry (title/channel/duration only).
        """
        duration = info.get("duration")
        if duration and duration > MAX_SONG_SECONDS:
            return None

        # 1. YouTube's own music metadata (present on most label uploads).
        track = info.get("track")
        artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
        if track and artists:
            artist = str(artists[0]).split(",")[0].strip()
            meta = await self.identify_track(f"{artist} {track}")
            if meta and matching.duration_plausible(duration, meta.duration):
                return meta
            return TrackMeta(artist=artist, title=track, album=info.get("album"),
                             duration=int(duration) if duration else None, source="youtube")

        # 2. Parse the title, and only trust it if a catalog confirms it.
        categories = info.get("categories")
        looks_like_music = categories is None or "Music" in categories
        min_cov = 0.7 if looks_like_music else 0.9
        title = matching.clean_video_title(info.get("title") or "")
        channel = matching.clean_channel(info.get("channel") or info.get("uploader"))
        attempts = [title]
        if channel:
            attempts.append(f"{channel} {title}")   # "Never Gonna Give You Up" on Rick Astley's channel
        for text in attempts:
            meta = await self.verify(text, duration=duration, min_query_cov=min_cov)
            if meta:
                print(f'[identify] video "{info.get("title")}" -> {meta}')
                return meta
        print(f'[identify] video "{info.get("title")}" is not a recognised song')
        return None
