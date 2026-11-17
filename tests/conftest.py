"""Fakes for everything external: Deezer, iTunes, Last.fm, YouTube, Discord voice.

The fakes answer in the same JSON / yt-dlp shapes the real services use, so
the parsing code is exercised too. Nothing in tests/ (outside tests/live)
touches the network or needs a Discord token.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matching  # noqa: E402
import metadata  # noqa: E402
import lastfm  # noqa: E402
import youtube  # noqa: E402
from player import GuildPlayer, Services  # noqa: E402

# --------------------------------------------------------------------------
# A tiny music catalog: (artist, title, album, seconds, popularity)
# --------------------------------------------------------------------------
CATALOG = [
    ("Radiohead", "Creep", "Pablo Honey", 238, 950000),
    ("Radiohead", "Creep (Acoustic)", "My Iron Lung", 259, 500000),
    ("Radiohead", "Karma Police", "OK Computer", 264, 900000),
    ("Radiohead", "No Surprises", "OK Computer", 229, 880000),
    ("Radiohead", "Paranoid Android", "OK Computer", 387, 870000),
    ("TLC", "Creep", "CrazySexyCool", 268, 700000),
    ("Kendrick Lamar", "HUMBLE.", "DAMN.", 177, 990000),
    ("Kendrick Lamar", "Money Trees (feat. Jay Rock)", "good kid, m.A.A.d city", 386, 940000),
    ("Kendrick Lamar", "Swimming Pools (Drank)", "good kid, m.A.A.d city", 313, 930000),
    ("Kendrick Lamar", "Bitch, Don't Kill My Vibe", "good kid, m.A.A.d city", 310, 920000),
    ("A$AP Rocky", "Praise The Lord (Da Shine) (feat. Skepta)", "TESTING", 205, 960000),
    ("AC/DC", "Thunderstruck", "The Razors Edge", 292, 970000),
    ("Oasis", "Live Forever", "Definitely Maybe", 276, 890000),
    ("Oasis", "Live Forever (Live at Knebworth)", "Knebworth 1996", 301, 300000),
    ("Queen", "Bohemian Rhapsody", "A Night at the Opera", 354, 980000),
    ("Queen", "Bohemian Rhapsody (Live Aid)", "Live Aid", 150, 400000),
    ("Rick Astley", "Never Gonna Give You Up", "Whenever You Need Somebody", 213, 975000),
    ("Madonna", "Like a Prayer", "Like a Prayer", 341, 910000),
    ("Madonna", "Vogue", "I'm Breathless", 316, 905000),
    ("Some Band", "Madonna", "Tribute Songs", 200, 1000),
    ("Nobody Famous", "Rain Sounds", "Sleep", 180, 4000),
    ("The Killers", "Mr. Brightside", "Hot Fuss", 222, 985000),
    ("Beyoncé", "Halo", "I Am... Sasha Fierce", 261, 965000),
    ("Sing King", "Halo (Karaoke Version)", "Karaoke Hits", 262, 999000),
    ("Tame Impala", "The Less I Know The Better", "Currents", 216, 955000),
    ("Part Time Cover Band", "Mr. Brightside", "Covers", 230, 360000),
    ("Obscure Act", "Filler One", "OK Computer", 200, 90000),
    ("Obscure Act", "Filler Two", "OK Computer", 210, 60000),
    ("Action Bronson", "Wolfpack", "Blue Chips 7000", 154, 500000),
]

# (id, artist, title, record_type, track indexes into CATALOG)
ALBUMS = [
    (107, "Obscure Act", "OK Computer", "album", [26, 27]),       # same title, listed first
    (101, "Radiohead", "OK Computer", "album", [2, 3, 4]),
    (102, "Radiohead", "OK Computer OKNOTOK 1997 2017 (Deluxe Edition)", "album", [2, 3, 4, 0]),
    (103, "Kendrick Lamar", "good kid, m.A.A.d city", "album", [7, 8, 9]),
    (104, "Kendrick Lamar", "good kid, m.A.A.d city (Deluxe)", "album", [7, 8, 9, 6]),
    (105, "Radiohead", "Creep", "single", [0]),
    (106, "Radiohead", "Pablo Honey", "album", [0]),
    (108, "Action Bronson", "Blue Chips 7000", "album", [28]),    # the mixtape "Blue Chips" is NOT here
]

# Mixtapes that only MusicBrainz knows: (id, artist credit, title, primary, secondary, [(title, ms)])
MB_ALBUMS = [
    ("mb-7000", [("Action Bronson", "")], "Blue Chips 7000", "Album", [], [("Wolfpack", 154000)]),
    ("mb-12", [("Action Bronson", "")], "Blue Chips 1 & 2", "Album", ["Compilation"], [("Pouches of Tuna", 200000)]),
    ("mb-bc", [("Action Bronson", " & "), ("Party Supplies", "")], "Blue Chips", "Album", ["Mixtape/Street"],
     [("Pouches of Tuna", 203000), ("Steve Wynn", 152000), ("Hookers at the Point", 251000)]),
]

SIMILAR = {  # Last.fm track.getsimilar, keyed by normalised "artist|title"
    "radiohead|creep": [("Radiohead", "Karma Police"), ("Radiohead", "No Surprises"),
                        ("Oasis", "Live Forever"), ("The Killers", "Mr. Brightside")],
    "radiohead|karma police": [("Radiohead", "Creep"), ("Radiohead", "Paranoid Android"),
                               ("Queen", "Bohemian Rhapsody")],
    "radiohead|no surprises": [("Radiohead", "Creep"), ("Tame Impala", "The Less I Know The Better")],
    "rick astley|never gonna give you up": [("Madonna", "Vogue"), ("Madonna", "Like a Prayer")],
    "the killers|mr brightside": [("AC/DC", "Thunderstruck")],
}
SIMILAR_ARTISTS = {"kendrick lamar": ["Radiohead"]}


def _matches_all_terms(query, *fields):
    """Deezer-like: every search term has to match something, and (to be
    pessimistic about the real APIs) misspelled terms match nothing."""
    hay = matching.tokens(" ".join(fields))
    hay += [a + b for a, b in zip(hay, hay[1:])]          # "acdc" finds "AC/DC"
    return all(q in hay for q in matching.tokens(query))


class ServiceUnavailable(Exception):
    status = 503


class FakeHttp:
    """Stands in for metadata.Http and answers like the real APIs."""

    def __init__(self):
        self.calls = []
        self.deezer_down = False
        self.deezer_empty = False
        self.itunes_down = False
        self.deezer_least_popular_first = False
        self.musicbrainz_503s = 0            # fail this many MusicBrainz requests first

    async def get_json(self, url, params=None, timeout=8, headers=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "musicbrainz.org" in url:
            assert headers and "User-Agent" in headers, "MusicBrainz requires a User-Agent"
            if self.musicbrainz_503s > 0:
                self.musicbrainz_503s -= 1
                raise ServiceUnavailable()
            return self._musicbrainz(url, params)
        if "api.deezer.com" in url:
            if self.deezer_down:
                raise ConnectionError("deezer unreachable")
            return self._deezer(url, params)
        if "itunes.apple.com" in url:
            if self.itunes_down:
                raise ConnectionError("itunes unreachable")
            return self._itunes(url, params)
        if "audioscrobbler" in url:
            return self._lastfm(params)
        raise AssertionError(f"unexpected URL {url}")

    # -- Deezer --------------------------------------------------------------
    def _deezer(self, url, params):
        if self.deezer_empty:
            return {"data": [], "total": 0}
        if url.endswith("/search"):
            hits = [(i, t) for i, t in enumerate(CATALOG) if _matches_all_terms(params["q"], t[0], t[1], t[2])]
            hits.sort(key=lambda h: h[1][4] if self.deezer_least_popular_first else -h[1][4])
            return {"data": [self._deezer_track(i, t) for i, t in hits[: params.get("limit", 25)]]}
        if url.endswith("/search/album"):
            hits = [a for a in ALBUMS if _matches_all_terms(params["q"], a[1], a[2])]
            return {"data": [{"id": a[0], "title": a[2], "nb_tracks": len(a[4]), "record_type": a[3],
                              "artist": {"name": a[1]}} for a in hits]}
        if "/album/" in url and url.endswith("/tracks"):
            album = next(a for a in ALBUMS if str(a[0]) == url.split("/")[-2])
            rows = []
            for pos, idx in enumerate(album[4], 1):
                row = self._deezer_track(idx, CATALOG[idx])
                row.pop("album")
                row.update(track_position=pos, disk_number=1)
                rows.append(row)
            return {"data": list(reversed(rows))}     # out of order on purpose
        return {"error": {"type": "DataException", "message": "no data", "code": 800}}

    @staticmethod
    def _deezer_track(i, t):
        return {"id": i, "title": t[1], "title_short": t[1].split(" (")[0], "duration": t[3],
                "rank": t[4], "explicit_lyrics": False, "artist": {"name": t[0]},
                "album": {"title": t[2]}}

    # -- MusicBrainz ---------------------------------------------------------
    @staticmethod
    def _musicbrainz(url, params):
        credit = lambda a: [{"name": n, "joinphrase": j} for n, j in a[1]]
        if url.endswith("/release-group"):
            hits = [a for a in MB_ALBUMS
                    if _matches_all_terms(params["query"], " ".join(n for n, _ in a[1]), a[2])]
            return {"release-groups": [{"id": a[0], "title": a[2], "primary-type": a[3],
                                        "secondary-types": a[4], "artist-credit": credit(a)} for a in hits]}
        album = next(a for a in MB_ALBUMS if a[0] == params["release-group"])
        tracks = [{"position": i, "title": t, "length": ms, "artist-credit": credit(album)}
                  for i, (t, ms) in enumerate(album[5], 1)]
        bonus = tracks + [{"position": 99, "title": "Bonus Cut", "length": 1000}]
        return {"releases": [{"title": album[2], "media": [{"tracks": bonus}]},        # odd pressing
                             {"title": album[2], "media": [{"tracks": tracks}]},
                             {"title": album[2], "media": [{"tracks": tracks}]}]}

    # -- iTunes --------------------------------------------------------------
    def _itunes(self, url, params):
        if url.endswith("/lookup"):
            album = next(a for a in ALBUMS if str(a[0]) == str(params["id"]))
            rows = [{"wrapperType": "collection", "collectionName": album[2]}]
            for pos, idx in enumerate(album[4], 1):
                t = CATALOG[idx]
                rows.append({"wrapperType": "track", "artistName": t[0], "trackName": t[1],
                             "collectionName": album[2], "trackTimeMillis": t[3] * 1000,
                             "trackNumber": pos, "discNumber": 1})
            return {"results": rows}
        if params.get("entity") == "album":
            hits = [a for a in ALBUMS if _matches_all_terms(params["term"], a[1], a[2])]
            return {"results": [{"collectionId": a[0], "artistName": a[1],
                                 "collectionName": a[2] + (" - Single" if a[3] == "single" else ""),
                                 "trackCount": len(a[4])} for a in hits]}
        hits = [t for t in CATALOG if _matches_all_terms(params["term"], t[0], t[1], t[2])]
        hits.sort(key=lambda t: -t[4])
        return {"results": [{"artistName": t[0], "trackName": t[1], "collectionName": t[2],
                             "trackTimeMillis": t[3] * 1000, "trackExplicitness": "notExplicit"}
                            for t in hits]}

    # -- Last.fm -------------------------------------------------------------
    def _lastfm(self, params):
        method = params["method"]
        if method == "track.getsimilar":
            key = f"{matching.normalize(params['artist'])}|{matching.normalize(params['track'])}"
            if key not in SIMILAR:
                return {"error": 6, "message": "Track not found"}
            rows = [{"name": t, "artist": {"name": a}, "duration": 0} for a, t in SIMILAR[key]]
            return {"similartracks": {"track": rows}}
        if method == "artist.getsimilar":
            names = SIMILAR_ARTISTS.get(matching.normalize(params["artist"]), [])
            return {"similarartists": {"artist": [{"name": n} for n in names]}}
        if method == "artist.gettoptracks":
            rows = [{"name": t[1], "artist": {"name": t[0]}} for t in CATALOG
                    if matching.normalize(t[0]) == matching.normalize(params["artist"])]
            return {"toptracks": {"track": rows}}
        raise AssertionError(method)


# --------------------------------------------------------------------------
# Fake YouTube. Order matters: like the real thing, the official music video
# tends to come first, and it is often NOT the upload we want.
# --------------------------------------------------------------------------
def v(vid, title, channel, duration, **extra):
    return dict(id=vid, title=title, channel=channel, duration=duration, **extra)


VIDEOS = [
    v("creep_mv", "Radiohead - Creep (Official Music Video)", "Radiohead", 242),
    v("creep_live", "Radiohead - Creep (Live at Glastonbury)", "BBC Music", 271),
    v("creep_topic", "Creep", "Radiohead - Topic", 238, track="Creep", artists=["Radiohead"]),
    v("creep_cover", "Creep - Radiohead (Cover by Someone)", "Someone", 240),
    v("creep_clean", "Radiohead - Creep (Clean)", "CleanEdits", 236),
    v("tlc_creep", "TLC - Creep (Official Video)", "TLCVEVO", 270),
    v("karma", "Radiohead - Karma Police", "Radiohead", 264),
    v("nosurprises", "Radiohead - No Surprises", "Radiohead", 229),
    v("paranoid", "Paranoid Android", "Radiohead - Topic", 387),
    v("okc_full", "Radiohead - OK Computer (Full Album)", "AlbumUploads", 3200),
    v("humble_mv", "Kendrick Lamar - HUMBLE.", "KendrickLamarVEVO", 183),
    v("humble_audio", "Kendrick Lamar - HUMBLE. (Official Audio)", "Kendrick Lamar", 177),
    v("moneytrees", "Money Trees", "Kendrick Lamar - Topic", 386),
    v("pools", "Swimming Pools (Drank)", "Kendrick Lamar - Topic", 313),
    v("vibe", "Bitch, Don't Kill My Vibe", "Kendrick Lamar - Topic", 310),
    v("rick", "Rick Astley - Never Gonna Give You Up (Official Video) [4K Remaster]",
      "Rick Astley", 213, categories=["Music"]),
    v("rick_notags", "Never Gonna Give You Up", "RickAstleyVEVO", 212, categories=["Music"]),
    v("zoo", "Me at the zoo", "jawed", 19, categories=["Film & Animation"]),
    v("lofi", "lofi hip hop radio - beats to relax/study to", "Lofi Girl", 7200, categories=["Music"]),
    v("podcast", "Radiohead Creep explained - why the song works | Music Theory Podcast",
      "Theory Guy", 1100, categories=["Education"]),
    v("brightside", "The Killers - Mr. Brightside (Official Music Video)", "TheKillersVEVO", 228),
    v("vogue", "Madonna - Vogue (Official Video)", "Madonna", 320),
    v("prayer", "Madonna - Like a Prayer (Official Audio)", "Madonna", 341),
    v("liveforever", "Oasis - Live Forever", "Oasis", 276),
    v("bohemian", "Queen - Bohemian Rhapsody (Official Video Remastered)", "Queen Official", 359),
    v("thunder", "AC/DC - Thunderstruck (Official Video)", "acdcVEVO", 293),
    v("tuna", "Action Bronson - Pouches of Tuna (feat. Roc Marciano)", "Fool's Gold", 203),
    v("wynn", "Steve Wynn", "Action Bronson - Topic", 152),
    v("hookers", "Action Bronson & Party Supplies - Hookers at the Point", "UploaderX", 250),
    v("tame", "Tame Impala - The Less I Know The Better (Official Audio)", "Tame Impala", 216),
]
PLAYLISTS = {
    "PLmusic": ["creep_mv", "zoo", "karma", "nosurprises", "brightside"],
    "PLjunk": ["zoo", "podcast", "lofi", "zoo"],
}
BROKEN = set()        # video ids whose stream extraction fails


class FakeYouTubeBackend:
    """Replaces the yt-dlp call inside youtube.YouTube."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, flat):
        self.calls.append((query, flat))
        if query.startswith("ytsearch"):
            limit, _, text = query.partition(":")
            limit = int(limit[len("ytsearch"):] or 1)
            q = matching.content_tokens(text)
            scored = []
            for i, vid in enumerate(VIDEOS):
                hay = matching.tokens(vid["title"] + " " + vid["channel"])
                hits = sum(1 for t in q if any(matching.token_match(t, h) for h in hay))
                if hits:
                    scored.append((-hits, i, vid))
            scored.sort()
            return {"_type": "playlist", "entries": [self._flat(s[2]) for s in scored[:limit]]}
        if "list=" in query:
            ids = PLAYLISTS[query.split("list=")[1]]
            return {"_type": "playlist", "title": "A playlist",
                    "entries": [self._flat(self._video(i)) for i in ids]}
        vid = self._video(query.split("v=")[1])
        if vid["id"] in BROKEN:
            raise RuntimeError("Video unavailable")
        return {**vid, "webpage_url": self._url(vid["id"]),
                "url": f"https://stream.example/{vid['id']}?n={len(self.calls)}"}

    @staticmethod
    def _url(vid):
        return f"https://www.youtube.com/watch?v={vid}"

    @staticmethod
    def _video(vid):
        return next(x for x in VIDEOS if x["id"] == vid)

    def _flat(self, vid):
        return {"id": vid["id"], "url": self._url(vid["id"]), "title": vid["title"],
                "channel": vid["channel"], "duration": vid["duration"], "ie_key": "Youtube"}


# --------------------------------------------------------------------------
# Fake Discord objects
# --------------------------------------------------------------------------
class FakeVoiceClient:
    def __init__(self, channel="voice-1"):
        self.channel = channel
        self.connected = True
        self.played = []          # every source handed to play()
        self._after = None

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self._after is not None

    def is_paused(self):
        return False

    def play(self, source, after=None):
        assert self._after is None, "play() called while already playing"
        self.played.append(source)
        self._after = after

    def stop(self):
        self.finish()

    def finish(self, error=None):
        """Simulate the current track ending."""
        after, self._after = self._after, None
        if after:
            after(error)

    async def disconnect(self):
        self.connected = False

    async def move_to(self, channel):
        self.channel = channel


class FakeVoiceChannel:
    def __init__(self, ctx):
        self._ctx = ctx

    async def connect(self, self_deaf=True):
        self._ctx.voice_client = FakeVoiceClient(self)
        return self._ctx.voice_client


class FakeCtx:
    def __init__(self, guild_id=1, author="brooks", in_voice=True):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.voice_client = None
        self.sent = []            # (content, embed)
        self.channel = self
        voice = types.SimpleNamespace(channel=FakeVoiceChannel(self)) if in_voice else None
        self.author = types.SimpleNamespace(name=author, voice=voice)

    async def send(self, content=None, embed=None):
        self.sent.append((content, embed))

    # helpers for assertions
    @property
    def embeds(self):
        return [e for _, e in self.sent if e is not None]

    @property
    def last(self):
        return self.embeds[-1]


def fields(embed):
    return {f.name: f.value for f in embed.fields}


async def settle(rounds=60):
    """Let background tasks (prefetch, autoplay refill) run to completion."""
    me, quiet = asyncio.current_task(), 0
    for _ in range(rounds * 20):
        await asyncio.sleep(0.001)
        busy = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        quiet = 0 if busy else quiet + 1
        if quiet >= 3:
            return


def played_ids(voice):
    return [src.rsplit("/", 1)[1].split("?")[0] for src in voice.played]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def yt_backend():
    BROKEN.clear()
    metadata.MusicBrainz.MIN_INTERVAL = 0        # no need to pace requests to a fake
    metadata.MusicBrainz.RETRY_DELAYS = (0, 0, 0)
    return FakeYouTubeBackend()


@pytest.fixture
def services(http, yt_backend):
    return Services(
        youtube=youtube.YouTube(extract=yt_backend),
        identifier=metadata.Identifier([metadata.Deezer(http), metadata.ITunes(http),
                                        metadata.MusicBrainz(http)]),
        lastfm=lastfm.LastFMClient(http, api_key="test"),
        make_source=lambda song: song.stream_url,
    )


@pytest.fixture
def identifier(services):
    return services.identifier


@pytest.fixture
def events():
    return []


@pytest.fixture
def player(services, events):
    async def on_event(player, kind, **data):
        events.append((kind, data))

    p = GuildPlayer(1, services, on_event)
    p.voice = FakeVoiceClient()
    return p


@pytest.fixture
def botmod(services, monkeypatch):
    """The real bot module, wired to the fake services."""
    import bot
    monkeypatch.setattr(bot, "services", services)
    bot.players.clear()
    yield bot
    bot.players.clear()


# --------------------------------------------------------------------------
# `--live` switch: tests in tests/live hit the real services and are skipped
# unless asked for.
# --------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="also run tests against the real Deezer/iTunes/Last.fm/YouTube")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test: run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
