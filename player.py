"""Per-server playback state machine: manual queue, autoplay queue, and what
plays next. Knows nothing about Discord commands or embeds; it talks to a
voice client (anything with play/stop/is_connected) and reports events
through a callback, which is what lets the tests drive it with fakes.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Callable

import matching
from models import Song, TrackMeta
from youtube import pick_best

AUTOPLAY = "Autoplay"
AUTOPLAY_BUFFER = 2          # autoplay songs kept looked-up ahead of time
MAX_FILL_ATTEMPTS = 12       # YouTube lookups per refill before giving up


@dataclass
class Services:
    youtube: Any
    identifier: Any
    lastfm: Any
    make_source: Callable[[Song], Any]


class GuildPlayer:
    def __init__(self, guild_id, services: Services, on_event=None):
        self.guild_id = guild_id
        self.services = services
        self.on_event = on_event            # async fn(player, kind, **data)
        self.voice = None                   # set by the bot on every command
        self.channel = None                 # where announcements go

        self.queue: list[Song] = []
        self.current: Song | None = None
        self.autoplay_queue: list[Song] = []
        self.autoplay_recs: list[TrackMeta] = []
        self.history: set[str] = set()      # track keys already played this session
        self.last_meta: TrackMeta | None = None
        self.suppress_next_announce = False

        self._pending_seeds: list[TrackMeta] = []
        self._reseeded: set[str] = set()
        self._gen = 0                       # bumped by stop(); stale callbacks are ignored
        self._seed_gen = 0                  # bumped by set_seeds()
        self._lock = asyncio.Lock()
        self._fill_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._loop = None

    # ------------------------------------------------------------------ utils
    def _voice_ready(self) -> bool:
        return self.voice is not None and self.voice.is_connected()

    async def _emit(self, kind, **data):
        if self.on_event:
            try:
                await self.on_event(self, kind, **data)
            except Exception as e:
                print(f"[player] event handler failed: {e!r}")

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    def _key(meta: TrackMeta | None):
        return matching.track_key(meta.artist, meta.title) if meta else None

    def _exclude_keys(self) -> set[str]:
        """Tracks autoplay must not pick: already played or already lined up."""
        keys = set(self.history)
        for song in [self.current, *self.queue, *self.autoplay_queue]:
            if song and song.meta:
                keys.add(self._key(song.meta))
        return keys

    # --------------------------------------------------------------- playback
    async def prepare(self, song: Song):
        """Make a song playable: find its video if needed, get a fresh stream URL."""
        yt = self.services.youtube
        async with song.lock:
            if song.url is None:
                if song.meta:
                    cand = await yt.find(song.meta, song.search_query)
                else:
                    cand = pick_best(await yt.search(song.search_query), query=song.search_query)
                if not cand:
                    raise LookupError(f"Couldn't find **{song.title}** on YouTube")
                song.url, song.title = cand.url, cand.title
                song.duration = cand.duration or song.duration
            if not song.stream_is_fresh():
                await yt.resolve(song)

    async def ensure_playing(self):
        """Start the next song if nothing is playing. Safe to call any time."""
        if self.current is None:
            await self._advance()

    async def _advance(self):
        self._loop = asyncio.get_running_loop()
        started, from_autoplay = None, False
        async with self._lock:
            if self.current is not None or not self._voice_ready():
                return
            gen = self._gen
            while True:
                if self.queue:
                    song, from_autoplay = self.queue.pop(0), False
                elif self.autoplay_queue:
                    song, from_autoplay = self.autoplay_queue.pop(0), True
                else:
                    self._spawn_autoplay_fill()
                    return
                self.current = song
                try:
                    await self.prepare(song)
                except asyncio.CancelledError:
                    self.current = None
                    raise
                except Exception as e:
                    if gen != self._gen:
                        return
                    self.current = None
                    print(f"[player] skipping {song.title}: {e!r}")
                    await self._emit("error", title="Skipping Track",
                                     text=str(e) if isinstance(e, LookupError)
                                     else f"Couldn't load **{song.title}**")
                    continue
                if gen != self._gen:            # stop() was called while loading
                    return
                if not self._voice_ready():
                    self.current = None
                    return
                try:
                    self.voice.play(self.services.make_source(song), after=self._after(gen))
                except Exception as e:
                    self.current = None
                    print(f"[player] voice.play failed: {e!r}")
                    await self._emit("error", title="Playback Error", text=str(e))
                    return
                started = song
                break

        if started.meta:
            self.history.add(self._key(started.meta))
            self.last_meta = started.meta
        announce = from_autoplay and not self.suppress_next_announce
        self.suppress_next_announce = False
        if announce:
            await self._emit("autoplay", song=started)
        self._background(self._after_start())

    def _after(self, gen):
        """Callback discord.py runs (on its audio thread) when a track ends."""
        def callback(error):
            asyncio.run_coroutine_threadsafe(self._on_track_end(error, gen), self._loop)
        return callback

    async def _on_track_end(self, error, gen):
        if gen != self._gen:
            return                              # the session this belonged to was stopped
        if error:
            print(f"[player] playback error: {error}")
            await self._emit("error", title="Playback Error", text=str(error))
        self.current = None
        await self._advance()

    async def _after_start(self):
        """While a song plays: get the next one ready and top up autoplay."""
        upcoming = self.queue[0] if self.queue else \
            self.autoplay_queue[0] if self.autoplay_queue else None
        if upcoming:
            try:
                await self.prepare(upcoming)
            except Exception as e:
                print(f"[player] prefetch failed for {upcoming.title}: {e!r}")
        self._spawn_autoplay_fill()

    async def stop(self):
        """Full reset (used by /disconnect)."""
        self._gen += 1
        self._seed_gen += 1
        self.queue.clear()
        self.autoplay_queue.clear()
        self.autoplay_recs.clear()
        self._pending_seeds.clear()
        self._reseeded.clear()
        self.history.clear()
        self.last_meta = None
        self.current = None
        self.suppress_next_announce = False
        for task in [self._fill_task, *self._tasks]:
            if task and not task.done():
                task.cancel()
        self._fill_task = None
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    # --------------------------------------------------------------- autoplay
    def set_seeds(self, seeds: list[TrackMeta]):
        """Point autoplay at new seed track(s) and rebuild its queue."""
        seeds = [s for s in seeds if s]
        if not seeds:
            return
        self._seed_gen += 1
        self._pending_seeds = seeds
        self._reseeded.clear()
        self.autoplay_recs.clear()
        self.autoplay_queue.clear()
        self._spawn_autoplay_fill()

    def _take_seeds(self) -> list[TrackMeta]:
        if self._pending_seeds:
            seeds, self._pending_seeds = self._pending_seeds, []
        elif self.last_meta and self._key(self.last_meta) not in self._reseeded:
            # Recommendations ran dry: keep going from the last song played.
            seeds = [self.last_meta]
        else:
            return []
        self._reseeded.update(self._key(s) for s in seeds)
        return seeds

    def _can_fill(self) -> bool:
        return bool(self._pending_seeds or self.autoplay_recs or
                    (self.last_meta and self._key(self.last_meta) not in self._reseeded))

    def _spawn_autoplay_fill(self):
        if self._fill_task and not self._fill_task.done():
            return
        if len(self.autoplay_queue) >= AUTOPLAY_BUFFER or not self._can_fill():
            return
        self._fill_task = asyncio.create_task(self._fill_autoplay())

    async def _fill_autoplay(self):
        gen = self._gen
        while gen == self._gen:
            added = await self._fill_once(gen)
            if not added or gen != self._gen:
                return
            await self.ensure_playing()         # kick playback if we were idle
            if len(self.autoplay_queue) >= AUTOPLAY_BUFFER:
                return

    async def _fill_once(self, gen) -> bool:
        """Look up autoplay songs until the buffer is full. True if any were added."""
        added, attempts = False, 0
        while len(self.autoplay_queue) < AUTOPLAY_BUFFER and attempts < MAX_FILL_ATTEMPTS:
            seed_gen = self._seed_gen
            if not self.autoplay_recs:
                seeds = self._take_seeds()
                if not seeds:
                    break
                recs = await self.services.lastfm.recommendations(
                    seeds, limit=25, exclude=self._exclude_keys())
                if gen != self._gen:
                    return False
                if seed_gen != self._seed_gen:
                    continue                    # seeds changed while we waited
                random.shuffle(recs)
                self.autoplay_recs.extend(recs)
                continue
            rec = self.autoplay_recs.pop(0)
            if self._key(rec) in self._exclude_keys():
                continue
            attempts += 1
            try:
                cand = await self.services.youtube.find(rec)
            except Exception as e:
                print(f"[player] autoplay lookup failed for {rec}: {e!r}")
                cand = None
            if gen != self._gen:
                return False
            if seed_gen != self._seed_gen or not cand:
                continue
            song = Song.from_candidate(cand, rec)
            song.requester = AUTOPLAY
            self.autoplay_queue.append(song)
            added = True
        return added
