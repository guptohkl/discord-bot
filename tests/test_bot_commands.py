"""Every Discord command, run through the real bot.py with fake Discord objects.

Commands are invoked the way discord.py would (`command.callback(ctx, ...)`),
against fake Deezer/Last.fm/YouTube and a fake voice connection.
"""
import pytest
from discord.ext import commands

from conftest import BROKEN, FakeCtx, fields, played_ids, settle


async def run(botmod, name, ctx, *args, **kwargs):
    await botmod.bot.get_command(name).callback(ctx, *args, **kwargs)
    await settle()


@pytest.fixture
def ctx():
    return FakeCtx()


def player_of(botmod, ctx):
    return botmod.players[ctx.guild.id]


# --------------------------------------------------------------------- /play
async def test_play_text_identifies_and_plays_album_audio(botmod, ctx):
    await run(botmod, "play", ctx, search="creep radiohead")
    assert played_ids(ctx.voice_client) == ["creep_topic"]         # not the music video
    embed = ctx.embeds[0]
    assert embed.title == "Track Queued"
    f = fields(embed)
    assert f["Identified As"] == "`Radiohead - Creep`"
    assert f["Autoplay"].startswith("✅")
    assert f["Duration"] == "3:58"
    assert "Position in Queue" not in f
    assert embed.footer.text == "Requested by brooks"
    assert len(player_of(botmod, ctx).autoplay_queue) == 2


async def test_play_second_song_is_queued_with_position(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    await run(botmod, "play", ctx, search="kendrick humble")
    f = fields(ctx.last)
    assert f["Position in Queue"] == "#1" and f["Estimated Start"] == "3:58"
    assert played_ids(ctx.voice_client) == ["creep_topic"]
    assert player_of(botmod, ctx).queue[0].url.endswith("humble_audio")
    # autoplay now follows the most recent song (Kendrick -> similar-artist fallback)
    assert {s.meta.artist for s in player_of(botmod, ctx).autoplay_queue} <= {"Kendrick Lamar", "Radiohead"}


async def test_play_typo_is_recovered_through_youtube(botmod, ctx):
    # Two typos: no catalog hit, but YouTube finds the video and the video is recognised.
    await run(botmod, "play", ctx, search="kendrik lamar humbel")
    f = fields(ctx.embeds[0])
    assert f["Identified As"] == "`Kendrick Lamar - HUMBLE.`" and f["Autoplay"].startswith("✅")


async def test_play_unidentifiable_text_still_plays_without_autoplay(botmod, ctx):
    await run(botmod, "play", ctx, search="me at the zoo")
    assert played_ids(ctx.voice_client) == ["zoo"]
    f = fields(ctx.embeds[0])
    assert "Identified As" not in f and f["Autoplay"].startswith("❌")
    assert player_of(botmod, ctx).autoplay_queue == []


async def test_play_nothing_found(botmod, ctx):
    await run(botmod, "play", ctx, search="qqqqzzzz xxxxyyyy")
    assert ctx.last.title == "Error" and "No song found" in ctx.last.description
    assert ctx.voice_client.played == []


async def test_play_youtube_link_of_a_song_autoplays(botmod, ctx):
    await run(botmod, "play", ctx, search="https://www.youtube.com/watch?v=rick")
    assert played_ids(ctx.voice_client) == ["rick"]
    f = fields(ctx.embeds[0])
    assert f["Identified As"] == "`Rick Astley - Never Gonna Give You Up`"
    assert {s.meta.artist for s in player_of(botmod, ctx).autoplay_queue} == {"Madonna"}


async def test_play_youtube_link_of_a_non_song_does_not_autoplay(botmod, ctx):
    await run(botmod, "play", ctx, search="https://www.youtube.com/watch?v=zoo")
    assert played_ids(ctx.voice_client) == ["zoo"]
    assert fields(ctx.embeds[0])["Autoplay"].startswith("❌")
    ctx.voice_client.finish()
    await settle()
    assert player_of(botmod, ctx).current is None


async def test_play_broken_link(botmod, ctx):
    BROKEN.add("karma")
    await run(botmod, "play", ctx, search="https://www.youtube.com/watch?v=karma")
    assert ctx.last.title == "Error"


async def test_play_playlist_link(botmod, ctx):
    await run(botmod, "play", ctx, search="https://www.youtube.com/playlist?list=PLmusic")
    p = player_of(botmod, ctx)
    assert played_ids(ctx.voice_client) == ["creep_mv"] and len(p.queue) == 4
    embed = ctx.embeds[0]
    assert embed.title == "Playlist Queued"
    f = fields(embed)
    assert f["Tracks Added"] == "5 songs" and f["Autoplay"] == "✅ Autoplaying from 3 identified tracks"
    # the non-song in the playlist was skipped as a seed; autoplay is lined up for afterwards
    assert len(p.autoplay_queue) == 2
    assert all(s.requester == "Autoplay" for s in p.autoplay_queue)


async def test_play_playlist_of_non_songs_has_no_autoplay(botmod, ctx):
    await run(botmod, "play", ctx, search="https://www.youtube.com/playlist?list=PLjunk")
    assert fields(ctx.embeds[0])["Autoplay"].startswith("❌")
    assert player_of(botmod, ctx).autoplay_queue == []


async def test_play_moves_to_callers_channel(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    first_channel = ctx.voice_client.channel
    ctx.author.voice.channel = type(first_channel)(ctx)
    await run(botmod, "play", ctx, search="radiohead karma police")
    assert ctx.voice_client.channel is ctx.author.voice.channel


async def test_autoplay_announces_itself_when_it_starts(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    ctx.voice_client.finish()
    await settle()
    embed = ctx.last
    assert embed.title == "Autoplaying Next Song" and embed.footer.text == "Provided by last.fm"
    assert "Track Info" in fields(embed)


# -------------------------------------------------------------------- /album
async def test_album_queues_tracks_in_order_and_plays_correct_audio(botmod, ctx):
    await run(botmod, "album", ctx, search="good kid maad city")
    p = player_of(botmod, ctx)
    embed = ctx.embeds[0]
    assert embed.title == "Album Queued" and "good kid, m.A.A.d city" in embed.description
    f = fields(embed)
    assert f["Tracks Added"] == "3 songs" and f["Duration"] == "16:49"
    assert played_ids(ctx.voice_client) == ["moneytrees"]
    assert [s.meta.title for s in p.queue] == ["Swimming Pools (Drank)", "Bitch, Don't Kill My Vibe"]

    for _ in range(2):
        ctx.voice_client.finish()
        await settle()
    assert played_ids(ctx.voice_client) == ["moneytrees", "pools", "vibe"]


async def test_album_then_autoplay_continues_with_similar_tracks_not_album_tracks(botmod, ctx):
    await run(botmod, "album", ctx, search="ok computer")
    p = player_of(botmod, ctx)
    for _ in range(3):
        ctx.voice_client.finish()
        await settle()
    assert p.current.requester == "Autoplay"
    assert p.current.meta.title not in {"Karma Police", "No Surprises", "Paranoid Android"}
    assert ctx.last.title == "Autoplaying Next Song"


async def test_album_not_found(botmod, ctx):
    await run(botmod, "album", ctx, search="zzzz no such album")
    assert ctx.last.title == "Error" and "No album found" in ctx.last.description


async def test_album_appends_behind_existing_queue(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    await run(botmod, "album", ctx, search="ok computer")
    assert played_ids(ctx.voice_client) == ["creep_topic"]
    assert len(player_of(botmod, ctx).queue) == 3


# --------------------------------------------------------------------- /skip
async def test_skip_shows_next_in_queue(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    await run(botmod, "play", ctx, search="radiohead karma police")
    await run(botmod, "skip", ctx)
    assert ctx.last.title == "Track Skipped" and "~~" in ctx.last.description
    assert "Karma Police" in fields(ctx.last)["Up Next"]
    assert played_ids(ctx.voice_client)[-1] == "karma"


async def test_skip_into_autoplay_announces_once(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    before = len(ctx.embeds)
    await run(botmod, "skip", ctx)
    new = ctx.embeds[before:]
    assert [e.title for e in new] == ["Track Skipped"]            # no duplicate autoplay embed
    assert "Up Next (Autoplay)" in fields(new[0])
    assert player_of(botmod, ctx).current.requester == "Autoplay"


async def test_skip_with_empty_queue(botmod, ctx):
    await run(botmod, "play", ctx, search="me at the zoo")
    await run(botmod, "skip", ctx)
    assert fields(ctx.last)["Up Next"] == "Queue is empty"
    assert player_of(botmod, ctx).current is None


async def test_skip_when_nothing_playing(botmod, ctx):
    await run(botmod, "skip", ctx)
    assert ctx.sent[-1][0] == "Nothing is playing!"


# ----------------------------------------------- /queue /shuffle /clear etc.
async def fill_queue(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    for q in ("radiohead karma police", "radiohead no surprises", "queen bohemian rhapsody"):
        await run(botmod, "play", ctx, search=q)


async def test_queue_lists_now_playing_queue_and_autoplay(botmod, ctx):
    await fill_queue(botmod, ctx)
    await run(botmod, "queue", ctx)
    text = ctx.last.description
    assert ctx.last.title == "Current Queue"
    assert "**Now Playing**" in text and "**1.**" in text and "**3.**" in text
    assert "Autoplay Queue" in text and "**A1.**" in text
    assert ctx.last.footer.text.startswith("3 songs • Total duration: 14:12")


async def test_queue_empty(botmod, ctx):
    await run(botmod, "queue", ctx)
    assert ctx.last.title == "Invalid Command" and ctx.last.description == "The queue is empty"


async def test_queue_shows_unresolved_album_tracks_and_stays_under_embed_limit(botmod, ctx):
    for _ in range(12):
        await run(botmod, "album", ctx, search="good kid maad city deluxe")
    await run(botmod, "queue", ctx)
    text = ctx.last.description
    assert "Kendrick Lamar - HUMBLE." in text and "more*" in text
    assert len(text) < 4096


async def test_shuffle(botmod, ctx):
    await fill_queue(botmod, ctx)
    before = set(map(id, player_of(botmod, ctx).queue))
    await run(botmod, "shuffle", ctx)
    assert ctx.last.title == "Queue Shuffled" and ctx.last.footer.text == "Shuffled 3 songs"
    assert set(map(id, player_of(botmod, ctx).queue)) == before


async def test_shuffle_errors_are_plain_text(botmod, ctx):
    await run(botmod, "shuffle", ctx)
    assert ctx.last.description == "The queue is empty - there is nothing to shuffle"
    await run(botmod, "play", ctx, search="radiohead creep")
    await run(botmod, "play", ctx, search="radiohead karma police")
    await run(botmod, "shuffle", ctx)
    assert ctx.last.description == "The queue is too small to shuffle"     # was a tuple before


async def test_clear(botmod, ctx):
    await fill_queue(botmod, ctx)
    await run(botmod, "clear", ctx)
    assert ctx.last.description == "Removed 3 songs from the queue"
    assert player_of(botmod, ctx).queue == [] and player_of(botmod, ctx).current is not None
    await run(botmod, "clear", ctx)
    assert ctx.last.title == "Invalid Command"


async def test_remove(botmod, ctx):
    await fill_queue(botmod, ctx)
    await run(botmod, "remove", ctx, 2)
    assert ctx.last.title == "Song Removed" and "No Surprises" in ctx.last.description
    assert ctx.last.footer.text == "2 songs remaining in queue"
    await run(botmod, "remove", ctx, 9)
    assert ctx.last.description == "Please choose a number between 1 and 2"


async def test_bump(botmod, ctx):
    await fill_queue(botmod, ctx)
    await run(botmod, "bump", ctx, 3)
    assert ctx.last.title == "Song Bumped to Top"
    assert "Bohemian Rhapsody" in player_of(botmod, ctx).queue[0].title
    await run(botmod, "bump", ctx, 1)
    assert ctx.last.description == "Please choose a number between 2 and 3"


# ---------------------------------------------------------------- the rest
async def test_disconnect_stops_everything(botmod, ctx):
    await fill_queue(botmod, ctx)
    voice = ctx.voice_client
    await run(botmod, "disconnect", ctx)
    p = player_of(botmod, ctx)
    assert not voice.connected and not voice.is_playing()
    assert p.queue == [] and p.autoplay_queue == [] and p.current is None
    assert ctx.sent[-1][0] == "DISCONNECTING! LATER DOOOOOOOOG!"
    assert len(voice.played) == 1                                # nothing restarted


async def test_disconnect_when_not_connected(botmod, ctx):
    await run(botmod, "disconnect", ctx)
    assert ctx.sent[-1][0] == "I'm not in a voice channel!"


async def test_play_again_after_disconnect(botmod, ctx):
    await run(botmod, "play", ctx, search="radiohead creep")
    await run(botmod, "disconnect", ctx)
    ctx.voice_client = None
    await run(botmod, "play", ctx, search="radiohead karma police")
    assert played_ids(ctx.voice_client) == ["karma"]


async def test_ping_autoplay_help(botmod, ctx):
    await run(botmod, "ping", ctx)
    assert ctx.sent[-1][0] == "Pong!"
    await run(botmod, "autoplay", ctx)
    assert ctx.last.title == "Autoplay"
    await run(botmod, "help", ctx)
    names = [f.name for f in ctx.last.fields]
    assert names == sorted(names) and "/album <album name>" in names and len(names) == 9


async def test_every_command_is_registered(botmod):
    assert {c.name for c in botmod.bot.commands} == {
        "ping", "disconnect", "play", "album", "skip", "queue", "shuffle",
        "clear", "remove", "bump", "autoplay", "help"}


async def test_voice_channel_check(botmod):
    ctx = FakeCtx(in_voice=False)
    check = botmod.bot.get_command("play").checks[0]
    assert await check(ctx) is False
    assert "must be in a voice channel" in ctx.sent[-1][0]
    assert await check(FakeCtx()) is True


async def test_bad_arguments_get_a_usage_message(botmod, ctx):
    ctx.command = botmod.bot.get_command("remove")
    await botmod.on_command_error(ctx, commands.BadArgument("not a number"))
    assert ctx.last.title == "Invalid Command" and "/remove <position>" in ctx.last.description
    before = len(ctx.sent)
    await botmod.on_command_error(ctx, commands.CheckFailure())
    assert len(ctx.sent) == before


async def test_servers_are_independent(botmod):
    a, b = FakeCtx(guild_id=1), FakeCtx(guild_id=2)
    await run(botmod, "play", a, search="radiohead creep")
    await run(botmod, "play", b, search="kendrick humble")
    assert played_ids(a.voice_client) == ["creep_topic"]
    assert played_ids(b.voice_client) == ["humble_audio"]
    await run(botmod, "disconnect", a)
    assert botmod.players[2].current is not None


@pytest.mark.parametrize("search, identified, video", [
    ("creep", "Radiohead - Creep", "creep_topic"),
    ("mr brightside", "The Killers - Mr. Brightside", "brightside"),
    ("never gonna give you up", "Rick Astley - Never Gonna Give You Up", "rick_notags"),
])
async def test_title_without_artist_is_identified_through_youtube(botmod, ctx, search, identified, video):
    await run(botmod, "play", ctx, search=search)
    assert played_ids(ctx.voice_client) == [video]
    f = fields(ctx.embeds[0])
    assert f["Identified As"] == f"`{identified}`" and f["Autoplay"].startswith("✅")


async def test_album_mixtape_plays_through_musicbrainz_fallback(botmod, ctx):
    await run(botmod, "album", ctx, search="action bronson - blue chips")
    embed = ctx.embeds[0]
    assert embed.title == "Album Queued" and "Blue Chips" in embed.description
    assert fields(embed)["Tracks Added"] == "3 songs"
    for _ in range(2):
        ctx.voice_client.finish()
        await settle()
    assert played_ids(ctx.voice_client) == ["tuna", "wynn", "hookers"]
