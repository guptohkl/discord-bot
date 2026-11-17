"""Choosing which YouTube upload to play, and the yt-dlp wrapper."""
import pytest

import youtube
from models import Candidate, Song, TrackMeta


def c(title, channel="", duration=None):
    return Candidate(url=f"https://www.youtube.com/watch?v={title}", title=title,
                     channel=channel, duration=duration)


CREEP = TrackMeta("Radiohead", "Creep", duration=238)


def test_duration_match_beats_music_video_and_live():
    cands = [c("Radiohead - Creep (Official Music Video)", "Radiohead", 290),
             c("Radiohead - Creep (Live at Glastonbury)", "BBC", 271),
             c("Radiohead - Creep (Lyrics)", "LyricChannel", 238)]
    assert youtube.pick_best(cands, meta=CREEP).title == "Radiohead - Creep (Lyrics)"


def test_topic_channel_preferred():
    cands = [c("Radiohead - Creep (Official Music Video)", "Radiohead", 239),
             c("Creep", "Radiohead - Topic", 238)]
    assert youtube.pick_best(cands, meta=CREEP).channel == "Radiohead - Topic"


@pytest.mark.parametrize("bad", ["Radiohead - Creep (Cover)", "Radiohead - Creep (Clean)",
                                 "Radiohead - Creep (Clean Version)", "Radiohead - Creep [Censored]",
                                 "Radiohead - Creep (Edited)",
                                 "Radiohead - Creep (Karaoke)", "Radiohead - Creep sped up",
                                 "Radiohead - Creep (Remix)", "Radiohead Creep REACTION"])
def test_wrong_versions_lose_even_when_listed_first(bad):
    cands = [c(bad, "Someone", 238), c("Radiohead - Creep", "Radiohead", 240)]
    assert youtube.pick_best(cands, meta=CREEP).title == "Radiohead - Creep"


def test_requested_version_is_not_penalised():
    meta = TrackMeta("Radiohead", "Creep (Acoustic)", duration=259)
    cands = [c("Radiohead - Creep", "Radiohead", 238), c("Radiohead - Creep (Acoustic)", "Radiohead", 259)]
    assert youtube.pick_best(cands, meta=meta).title == "Radiohead - Creep (Acoustic)"


def test_live_in_song_name_is_fine():
    meta = TrackMeta("Oasis", "Live Forever", duration=276)
    assert youtube.pick_best([c("Oasis - Live Forever", "Oasis", 276)], meta=meta) is not None


def test_full_album_upload_is_not_picked_for_a_track():
    meta = TrackMeta("Radiohead", "Karma Police", duration=264)
    cands = [c("Radiohead - OK Computer (Full Album) Karma Police", "x", 3200),
             c("Radiohead - Karma Police", "Radiohead", 264)]
    assert youtube.pick_best(cands, meta=meta).duration == 264


def test_returns_none_when_nothing_contains_the_title():
    cands = [c("Kendrick Lamar - HUMBLE.", "Kendrick", 177)]
    assert youtube.pick_best(cands, meta=CREEP) is None
    assert youtube.pick_best([], meta=CREEP) is None


def test_missing_durations_do_not_crash():
    cands = [c("Radiohead - Creep", "Radiohead", None)]
    assert youtube.pick_best(cands, meta=CREEP) is not None
    assert youtube.pick_best(cands, meta=TrackMeta("Radiohead", "Creep")) is not None


def test_raw_query_keeps_youtube_order_but_avoids_junk():
    cands = [c("Some Song (Clean Version)", "x", 200), c("Some Song (Official Music Video)", "x", 230),
             c("Some Song (Lyrics)", "x", 200)]
    assert youtube.pick_best(cands, query="some song").title == "Some Song (Lyrics)"
    # ...unless the junk word is what was asked for
    assert youtube.pick_best(cands, query="some song clean version").title == "Some Song (Clean Version)"


def test_raw_query_long_video_only_when_asked():
    cands = [c("lofi hip hop radio", "Lofi Girl", 7200), c("lofi hip hop song", "x", 180)]
    assert youtube.pick_best(cands, query="lofi hip hop").duration == 180
    assert youtube.pick_best(cands, query="lofi hip hop radio").duration == 7200


def test_is_url():
    assert youtube.is_url("https://youtu.be/abc")
    assert youtube.is_url("http://youtube.com/watch?v=abc")
    assert not youtube.is_url("radiohead creep")
    assert not youtube.is_url("https is a protocol")


def test_candidate_from_entry_variants():
    assert Candidate.from_entry({"id": "abc", "title": "T", "uploader": "U", "duration": 12.0}) == \
        Candidate("https://www.youtube.com/watch?v=abc", "T", "U", 12)
    assert Candidate.from_entry({"id": "abc", "url": "abc", "title": "T"}).url.endswith("v=abc")


# ------------------------------------------------------------ yt-dlp wrapper
async def test_search_is_flat_and_returns_candidates(services, yt_backend):
    cands = await services.youtube.search("radiohead creep")
    assert yt_backend.calls == [("ytsearch6:radiohead creep", True)]
    assert cands and all(isinstance(x, Candidate) and x.duration for x in cands)


async def test_find_picks_the_album_audio(services):
    cand = await services.youtube.find(CREEP)
    assert cand.url.endswith("creep_topic")


async def test_fetch_url_distinguishes_video_and_playlist(services):
    kind, info = await services.youtube.fetch_url("https://www.youtube.com/watch?v=rick")
    assert kind == "video" and info["url"].startswith("https://stream.example/")
    kind, info = await services.youtube.fetch_url("https://www.youtube.com/playlist?list=PLmusic")
    assert kind == "playlist" and len(info["entries"]) == 5


async def test_resolve_sets_stream_url(services):
    song = Song(title="x", url="https://www.youtube.com/watch?v=karma")
    assert not song.stream_is_fresh()
    await services.youtube.resolve(song)
    assert song.stream_is_fresh() and song.duration == 264 and song.title == "Radiohead - Karma Police"


def test_real_ytdlp_options_build():
    """The option dicts must be accepted by the installed yt-dlp."""
    import yt_dlp
    for flat in (True, False):
        opts = dict(youtube._BASE_OPTS)
        opts.update({"extract_flat": "in_playlist"} if flat else {"noplaylist": True})
        with yt_dlp.YoutubeDL(opts):
            pass
