"""Live checks against the real services. Run on the server with:

    docker compose run --rm discord-music-bot python -m pytest --live tests/live -v

These confirm what the offline tests cannot: that Deezer / iTunes / Last.fm /
YouTube still answer in the shapes the bot expects, and that a real stream
plays through ffmpeg. No Discord connection is made.
"""
import os
import shutil
import subprocess

import pytest
from dotenv import load_dotenv

import lastfm
import matching
import metadata
import youtube
from models import Song, TrackMeta

pytestmark = pytest.mark.live
load_dotenv()

RICK_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"       # a song
ZOO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"        # "Me at the zoo": not a song
# Any public playlist works; override with LIVE_PLAYLIST_URL if this one goes away.
PLAYLIST_URL = os.getenv("LIVE_PLAYLIST_URL",
                         "https://www.youtube.com/playlist?list=UUuAXFkgsw1L7xaCfnd5JJOw")

TRACK_QUERIES = [
    ("radiohead creep", "radiohead", "creep"),
    ("creep radiohead", "radiohead", "creep"),
    ("Radiohead - Creep", "radiohead", "creep"),
    ("kendrick humble", "kendrick lamar", "humble"),
    ("asap rocky praise the lord", "asap rocky", "praise the lord"),
    ("acdc thunderstruck", "ac dc", "thunderstruck"),
    ("bohemian rhapsody queen lyrics", "queen", "bohemian rhapsody"),
    ("tame impala the less i know the better official audio", "tame impala", "less i know the better"),
    ("kendrik lamar money trees", "kendrick lamar", "money trees"),          # typo
    ("fleetwood mac dreams", "fleetwood mac", "dreams"),
    ("daft punk get lucky", "daft punk", "get lucky"),
]
ALBUM_QUERIES = [
    ("good kid maad city", "kendrick lamar", 12),
    ("pink floyd dark side of the moon", "pink floyd", 9),
    ("rumours fleetwood mac", "fleetwood mac", 11),
    ("radiohead ok computer", "radiohead", 12),
    ("random access memories", "daft punk", 13),
    ("action bronson - dr lecter", "action bronson", 15),       # mixtape: MusicBrainz only
]


@pytest.fixture
async def http():
    h = metadata.Http()
    yield h
    await h.close()


@pytest.fixture
def identifier(http):
    return metadata.Identifier([metadata.Deezer(http), metadata.ITunes(http), metadata.MusicBrainz(http)])


@pytest.fixture
def yt():
    return youtube.YouTube()


def assert_track(meta, artist, title, query):
    assert meta is not None, f"not identified: {query!r}"
    assert artist in matching.normalize(meta.artist), f"{query!r} -> {meta}"
    assert title in matching.normalize(meta.title), f"{query!r} -> {meta}"


# ------------------------------------------------------------- recognition
@pytest.mark.parametrize("query, artist, title", TRACK_QUERIES)
async def test_identify_track(identifier, query, artist, title):
    meta = await identifier.identify_track(query)
    assert_track(meta, artist, title, query)
    assert meta.duration, "provider should give a duration"
    print(f"\n  {query!r} -> {meta} [popularity {meta.popularity}]")


@pytest.mark.parametrize("provider_cls", [metadata.Deezer, metadata.ITunes])
async def test_each_provider_alone(http, provider_cls):
    """Both the primary and the fallback must work on their own."""
    ident = metadata.Identifier([provider_cls(http)])
    assert_track(await ident.identify_track("radiohead creep"), "radiohead", "creep", "radiohead creep")
    album = await ident.identify_album("radiohead ok computer")
    assert album and len(album.tracks) >= 12 and all(t.title for t in album.tracks)


@pytest.mark.parametrize("query, artist, title", [
    ("mr brightside", "killers", "mr brightside"),
    ("never gonna give you up", "rick astley", "never gonna give you up"),
    ("creep", "radiohead", "creep"),
    ("bohemian rhapsody", "queen", "bohemian rhapsody"),
])
async def test_title_without_artist_goes_through_youtube(query, artist, title):
    """The full /play path: no artist typed, so YouTube's top result decides."""
    import bot
    try:
        song, meta = await bot.find_song(query)
    finally:
        await bot.http.close()
    assert song is not None, f"nothing found for {query!r}"
    print(f"\n  {query!r} -> video {song.title!r} -> {meta}")
    assert_track(meta, artist, title, query)


async def test_nonsense_is_not_identified(identifier):
    assert await identifier.identify_track("asdkjhq wlekjzx mcnbqpw") is None


@pytest.mark.parametrize("query, artist, min_tracks", ALBUM_QUERIES)
async def test_identify_album(identifier, query, artist, min_tracks):
    album = await identifier.identify_album(query)
    assert album is not None, f"album not identified: {query!r}"
    assert artist in matching.normalize(album.artist), f"{query!r} -> {album.artist} - {album.title}"
    assert len(album.tracks) >= min_tracks, f"{album.title}: only {len(album.tracks)} tracks"
    assert all(t.duration for t in album.tracks)
    print(f"\n  {query!r} -> {album.artist} - {album.title} ({len(album.tracks)} tracks, {album.source})")


async def test_mixtape_with_a_longer_named_sibling(identifier):
    """ "blue chips" must be the mixtape, not "Blue Chips 7000" or the "1 & 2" compilation."""
    album = await identifier.identify_album("action bronson blue chips")
    assert album is not None
    print(f"\n  -> {album.artist} - {album.title} ({len(album.tracks)} tracks, {album.source})")
    assert matching.normalize(album.title) == "blue chips" and len(album.tracks) >= 10


async def test_mixtape_tracks_are_on_youtube(identifier, yt):
    album = await identifier.identify_album("action bronson - dr lecter")
    found = 0
    for track in album.tracks[:4]:
        cand = await yt.find(track)
        print(f"\n  {track} [{track.duration}s] -> "
              f"{f'{cand.title} | {cand.channel} [{cand.duration}s]' if cand else 'NOT FOUND'}")
        found += bool(cand)
    assert found >= 3


# ------------------------------------------------------------------ Last.fm
needs_lastfm = pytest.mark.skipif(not os.getenv("LASTFM_API_KEY"), reason="LASTFM_API_KEY not set")


@needs_lastfm
async def test_lastfm_similar_tracks(http):
    recs = await lastfm.LastFMClient(http).recommendations([TrackMeta("Radiohead", "Creep")])
    assert len(recs) >= 10 and all(r.artist and r.title for r in recs)


@needs_lastfm
async def test_lastfm_artist_fallback(http):
    recs = await lastfm.LastFMClient(http).recommendations(
        [TrackMeta("Radiohead", "Definitely Not A Real Song 12345")])
    assert len(recs) >= 10


@needs_lastfm
async def test_lastfm_multiple_seeds(http):
    seeds = [TrackMeta("Kendrick Lamar", "Money Trees"), TrackMeta("Kendrick Lamar", "Swimming Pools (Drank)")]
    recs = await lastfm.LastFMClient(http).recommendations(seeds)
    keys = [matching.track_key(r.artist, r.title) for r in recs]
    assert len(recs) >= 10 and len(keys) == len(set(keys))


# ------------------------------------------------------------------ YouTube
async def test_youtube_search_returns_durations_and_channels(yt):
    cands = await yt.search("radiohead creep")
    assert len(cands) >= 3
    assert all(c.url.startswith("http") and c.title for c in cands)
    assert sum(1 for c in cands if c.duration) >= 3, "flat search results lack durations"
    assert sum(1 for c in cands if c.channel) >= 3, "flat search results lack channel names"


@pytest.mark.parametrize("query", ["radiohead creep", "kendrick lamar humble", "queen bohemian rhapsody"])
async def test_picked_video_matches_the_real_track_length(identifier, yt, query):
    meta = await identifier.identify_track(query)
    cand = await yt.find(meta)
    assert cand is not None, f"nothing on YouTube for {meta}"
    print(f"\n  {meta} [{meta.duration}s] -> {cand.title} | {cand.channel} [{cand.duration}s]")
    assert abs(cand.duration - meta.duration) <= 15


async def test_album_tracks_resolve_to_matching_videos(identifier, yt):
    album = await identifier.identify_album("radiohead ok computer")
    for track in album.tracks[:3]:
        cand = await yt.find(track)
        assert cand is not None, f"nothing on YouTube for {track}"
        print(f"\n  {track} [{track.duration}s] -> {cand.title} | {cand.channel} [{cand.duration}s]")
        assert abs(cand.duration - track.duration) <= 15


async def test_song_link_is_identified(identifier, yt):
    kind, info = await yt.fetch_url(RICK_URL)
    assert kind == "video" and info.get("url"), "single video should come back fully resolved"
    meta = await identifier.identify_video(info)
    assert_track(meta, "rick astley", "never gonna give you up", RICK_URL)


async def test_non_song_link_is_rejected(identifier, yt):
    kind, info = await yt.fetch_url(ZOO_URL)
    assert kind == "video"
    assert await identifier.identify_video(info) is None


async def test_playlist_link(yt):
    kind, info = await yt.fetch_url(PLAYLIST_URL)
    assert kind == "playlist" and len(info["entries"]) >= 3
    cands = [youtube.Candidate.from_entry(e) for e in info["entries"]]
    assert all(c.url.startswith("http") and c.title for c in cands)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_stream_actually_plays_through_ffmpeg(yt):
    """End to end minus Discord: resolve a stream and decode 3 seconds of it."""
    song = Song(title="x", url=RICK_URL)
    await yt.resolve(song)
    assert song.stream_is_fresh()
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
         "-reconnect_delay_max", "5", "-i", song.stream_url, "-t", "3", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-500:]
