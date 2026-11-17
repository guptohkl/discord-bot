"""Last.fm recommendations (async, so it no longer blocks the bot while it waits).

Lookups go by artist + track name with Last.fm's autocorrect. The old MBID
path is gone: MusicBrainz recording IDs rarely matched what Last.fm had, so
most lookups were already falling through to names (or to nothing, since the
bot refused to autoplay without an MBID).
"""
from __future__ import annotations

import os
import random

import matching
from models import TrackMeta

API = "https://ws.audioscrobbler.com/2.0/"


def _as_list(value):
    """Last.fm returns a bare object instead of a list when there is one item."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class LastFMClient:
    def __init__(self, http, api_key=None):
        self.http = http
        self.api_key = api_key or os.getenv("LASTFM_API_KEY")

    async def _call(self, method, **params):
        params.update(method=method, api_key=self.api_key, format="json", autocorrect=1)
        try:
            data = await self.http.get_json(API, params)
        except Exception as e:
            print(f"[lastfm] {method} failed: {e!r}")
            return {}
        if not isinstance(data, dict) or data.get("error"):
            return {}
        return data

    @staticmethod
    def _tracks(rows):
        out = []
        for t in _as_list(rows):
            name = t.get("name")
            artist = t.get("artist")
            artist = artist.get("name") if isinstance(artist, dict) else artist
            if name and artist:
                dur = t.get("duration")
                out.append(TrackMeta(artist=artist, title=name, source="lastfm",
                                     duration=int(dur) if dur and str(dur).isdigit() and int(dur) > 0 else None))
        return out

    async def similar_tracks(self, artist, title, limit=25):
        data = await self._call("track.getsimilar", artist=artist, track=title, limit=limit)
        return self._tracks((data.get("similartracks") or {}).get("track"))

    async def top_tracks(self, artist, limit=20):
        data = await self._call("artist.gettoptracks", artist=artist, limit=limit)
        return self._tracks((data.get("toptracks") or {}).get("track"))

    async def similar_artist_tracks(self, artist, limit=25):
        """Fallback: a random sample of top tracks from the artist and 4 similar ones."""
        data = await self._call("artist.getsimilar", artist=artist, limit=4)
        names = [artist] + [a.get("name") for a in
                            _as_list((data.get("similarartists") or {}).get("artist")) if a.get("name")]
        pool = []
        for name in names:
            pool.extend(await self.top_tracks(name))
        random.shuffle(pool)
        return pool[:limit]

    async def recommendations_for(self, seed: TrackMeta, limit=25):
        recs = await self.similar_tracks(seed.artist, seed.title, limit)
        if recs:
            print(f"[lastfm] {len(recs)} similar tracks for {seed}")
            return recs
        recs = await self.similar_artist_tracks(seed.artist, limit)
        print(f"[lastfm] {len(recs)} tracks via similar artists for {seed.artist}")
        return recs

    async def recommendations(self, seeds: list[TrackMeta], limit=25,
                              exclude: set[str] | None = None) -> list[TrackMeta]:
        """Recommendations for one or more seed tracks.

        With several seeds (an album, a playlist) the lists are interleaved
        so the result reflects all of them, then de-duplicated. `exclude`
        holds track keys that must not come back (already played / queued).
        """
        seen = set(exclude or ())
        seen.update(matching.track_key(s.artist, s.title) for s in seeds)
        lists = [await self.recommendations_for(seed, limit) for seed in seeds]
        merged = []
        for i in range(max((len(l) for l in lists), default=0)):
            for l in lists:
                if i < len(l):
                    key = matching.track_key(l[i].artist, l[i].title)
                    if key not in seen:
                        seen.add(key)
                        merged.append(l[i])
        return merged[:limit]
