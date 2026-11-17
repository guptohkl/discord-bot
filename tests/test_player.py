"""The playback state machine, driven with a fake voice client."""
import time

import matching
import models
from conftest import BROKEN, FakeVoiceClient, played_ids, settle
from models import Song, TrackMeta

CREEP = TrackMeta("Radiohead", "Creep", duration=238)
KARMA = TrackMeta("Radiohead", "Karma Police", duration=264)
RICK = TrackMeta("Rick Astley", "Never Gonna Give You Up", duration=213)


def url_song(vid, meta=None):
    return Song(title=vid, url=f"https://www.youtube.com/watch?v={vid}", meta=meta)


async def test_plays_queue_in_order(player):
    player.queue += [url_song("karma"), url_song("nosurprises"), url_song("paranoid")]
    await player.ensure_playing()
    assert player.current.title == "Radiohead - Karma Police" and len(player.queue) == 2
    for _ in range(2):
        player.voice.finish()
        await settle()
    assert played_ids(player.voice) == ["karma", "nosurprises", "paranoid"]
    player.voice.finish()
    await settle()
    assert player.current is None and not player.voice.is_playing()


async def test_ensure_playing_does_not_interrupt(player):
    player.queue.append(url_song("karma"))
    await player.ensure_playing()
    player.queue.append(url_song("paranoid"))
    await player.ensure_playing()
    assert played_ids(player.voice) == ["karma"] and len(player.queue) == 1


async def test_album_track_is_matched_to_youtube_only_when_needed(player, yt_backend):
    player.queue += [Song.from_meta(CREEP), Song.from_meta(KARMA)]
    assert player.queue[0].url is None
    await player.ensure_playing()
    # Picked the album audio (duration match + Topic channel), not the music video
    assert played_ids(player.voice) == ["creep_topic"]
    await settle()
    # ...and the next track was prefetched while the first one plays
    assert player.queue[0].url.endswith("karma") and player.queue[0].stream_is_fresh()


async def test_unplayable_song_is_skipped_with_a_message(player, events):
    BROKEN.add("karma")
    player.queue += [url_song("karma"), url_song("paranoid")]
    await player.ensure_playing()
    assert played_ids(player.voice) == ["paranoid"]
    assert events[0][0] == "error" and events[0][1]["title"] == "Skipping Track"


async def test_track_not_on_youtube_is_skipped(player, events):
    player.queue += [Song.from_meta(TrackMeta("Nobody", "Zzyzx Unfindable")), url_song("karma")]
    await player.ensure_playing()
    assert played_ids(player.voice) == ["karma"]
    assert "Couldn't find" in events[0][1]["text"]


async def test_expired_stream_url_is_refreshed_before_playing(player, yt_backend):
    song = url_song("karma")
    await player.services.youtube.resolve(song)
    old_stream = song.stream_url
    song.stream_fetched_at = time.time() - models.STREAM_MAX_AGE - 1     # queued hours ago
    player.queue.append(song)
    await player.ensure_playing()
    assert player.voice.played[0] != old_stream


async def test_fresh_stream_url_is_not_refetched(player, yt_backend):
    song = url_song("karma")
    await player.services.youtube.resolve(song)
    calls = len(yt_backend.calls)
    player.queue.append(song)
    await player.ensure_playing()
    assert len(yt_backend.calls) == calls


async def test_playback_error_is_reported_and_queue_continues(player, events):
    player.queue += [url_song("karma"), url_song("paranoid")]
    await player.ensure_playing()
    player.voice.finish(error=RuntimeError("ffmpeg died"))
    await settle()
    assert ("error", {"title": "Playback Error", "text": "ffmpeg died"}) in events
    assert played_ids(player.voice) == ["karma", "paranoid"]


async def test_nothing_plays_without_a_voice_connection(player):
    player.voice.connected = False
    player.queue.append(url_song("karma"))
    await player.ensure_playing()
    assert player.voice.played == [] and len(player.queue) == 1


# ------------------------------------------------------------------ autoplay
async def test_autoplay_takes_over_when_queue_ends(player, events):
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    assert len(player.autoplay_queue) == 2
    assert all(s.requester == "Autoplay" and s.meta for s in player.autoplay_queue)

    player.voice.finish()
    await settle()
    assert player.current.requester == "Autoplay"
    assert events[-1][0] == "autoplay" and events[-1][1]["song"] is player.current
    assert len(player.autoplay_queue) == 2          # buffer topped back up


async def test_manual_queue_has_priority_over_autoplay(player):
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    player.queue.append(url_song("rick", RICK))
    player.voice.finish()
    await settle()
    assert player.current.requester != "Autoplay" and played_ids(player.voice)[-1] == "rick"


async def test_autoplay_never_repeats_a_song(player):
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    seen = []
    for _ in range(12):
        if not player.voice.is_playing():
            break
        player.voice.finish()
        await settle()
        if player.current:
            seen.append(matching.track_key(player.current.meta.artist, player.current.meta.title))
    assert len(seen) == len(set(seen)) and len(seen) >= 5
    assert matching.track_key("Radiohead", "Creep") not in seen


async def test_autoplay_reseeds_from_last_song_when_recommendations_run_out(player):
    # Creep's 4 recommendations run out; the chain continues via the songs it played.
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    artists = set()
    for _ in range(10):
        if not player.voice.is_playing():
            break
        player.voice.finish()
        await settle()
        if player.current:
            artists.add(player.current.meta.artist)
    assert artists - {"Radiohead", "Oasis", "The Killers"}, artists     # got beyond the first list


async def test_autoplay_starts_by_itself_if_bot_was_idle(player):
    player.set_seeds([RICK])
    await settle()
    assert player.current and player.current.requester == "Autoplay"
    assert player.current.meta.artist == "Madonna"


async def test_new_seeds_replace_the_old_autoplay_queue(player):
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    player.set_seeds([RICK])
    await settle()
    assert {s.meta.artist for s in player.autoplay_queue} == {"Madonna"}


async def test_no_seeds_no_autoplay(player):
    player.queue.append(url_song("zoo"))
    await player.ensure_playing()
    player.set_seeds([])
    await settle()
    player.voice.finish()
    await settle()
    assert player.current is None and player.autoplay_queue == []


async def test_suppress_next_announce(player, events):
    player.queue.append(url_song("creep_topic", CREEP))
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    player.suppress_next_announce = True
    player.voice.finish()
    await settle()
    assert not [e for e in events if e[0] == "autoplay"]
    player.voice.finish()
    await settle()
    assert len([e for e in events if e[0] == "autoplay"]) == 1


# --------------------------------------------------------------------- stop
async def test_stop_resets_everything_and_nothing_restarts(player):
    player.queue += [url_song("creep_topic", CREEP), url_song("karma")]
    await player.ensure_playing()
    player.set_seeds([CREEP])
    await settle()
    await player.stop()
    await settle()
    assert player.current is None and not player.queue and not player.autoplay_queue
    assert not player.autoplay_recs and not player.voice.is_playing()
    assert played_ids(player.voice) == ["creep_topic"]


async def test_can_play_again_after_stop(player):
    player.queue.append(url_song("karma"))
    await player.ensure_playing()
    await player.stop()
    player.voice = FakeVoiceClient()
    player.queue.append(url_song("paranoid"))
    await player.ensure_playing()
    assert played_ids(player.voice) == ["paranoid"]
