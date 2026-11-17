import asyncio
import os
import random

import discord
from discord.ext import commands
from dotenv import load_dotenv

import lastfm
import metadata
import youtube
from models import Song
from player import AUTOPLAY, GuildPlayer, Services

### SETUP ###
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

QUEUE_DISPLAY_LIMIT = 15   # Discord embeds cap out at 4096 characters
PLAYLIST_SEED_SCAN = 8     # playlist entries checked when looking for autoplay seeds
MAX_SEEDS = 3              # tracks used to seed autoplay for albums/playlists


def make_audio_source(song):
    return discord.FFmpegPCMAudio(
        song.stream_url,
        before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        options='-vn -af "loudnorm=I=-16:TP=-1.5:LRA=11"'
    )


# CLIENTS - tests replace the attributes on `services` with fakes
http = metadata.Http()
services = Services(
    youtube=youtube.YouTube(),
    identifier=metadata.Identifier([metadata.Deezer(http), metadata.ITunes(http),
                                   metadata.MusicBrainz(http)]),
    lastfm=lastfm.LastFMClient(http),
    make_source=make_audio_source,
)

# SERVER SPECIFIC STATE
players = {}


### METHODS ###
def in_voice_channel():
    async def predicate(ctx):
        if not ctx.author.voice:
            await ctx.send("❌ You must be in a voice channel to use this command!")
            return False
        return True
    return commands.check(predicate)


def get_player(ctx):
    player = players.get(ctx.guild.id)
    if not player:
        player = players[ctx.guild.id] = GuildPlayer(ctx.guild.id, services, on_player_event)
    player.voice = ctx.voice_client
    player.channel = ctx.channel
    return player


async def on_player_event(player, kind, song=None, title=None, text=None):
    """Messages the player sends on its own (not in reply to a command)."""
    if not player.channel:
        return
    if kind == "autoplay":
        await player.channel.send(embed=create_song_embed(player, song))
    elif kind == "error":
        await player.channel.send(embed=create_embed(title, text, discord.Color.red()))


async def connect_to_voice(ctx):
    voice_channel = ctx.author.voice.channel
    if not ctx.voice_client:
        await voice_channel.connect(self_deaf=True)
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    # GUARD: Connection may have failed after retries (e.g. DAVE protocol rejection)
    if not ctx.voice_client:
        await ctx.send(embed=create_embed("Error", "Failed to connect to voice channel.", discord.Color.red()))
        return False
    return True


def fmt_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def song_link(song):
    # Album/autoplay tracks have no YouTube link until just before they play
    return f"[{song.title}]({song.url})" if song.url else song.title


async def find_playlist_seeds(entries, songs):
    """Identify a few real songs in a YouTube playlist to seed autoplay from."""
    seeds = []
    for entry, song in list(zip(entries, songs))[:PLAYLIST_SEED_SCAN]:
        meta = await services.identifier.identify_video(entry)
        if meta:
            song.meta = meta
            seeds.append(meta)
            if len(seeds) >= MAX_SEEDS:
                break
    return seeds


### MESSAGE EMBEDS
def create_embed(title, description=None, color=discord.Color.blue(), footer=None):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=footer)
    return embed


def add_queue_position_fields(embed, player, song):
    if song not in player.queue:
        return
    position = player.queue.index(song) + 1
    embed.add_field(name="Position in Queue", value=f"#{position}", inline=True)

    # TIME UNTIL PLAY
    time_until = sum(s.duration or 0 for s in player.queue[:position - 1])
    if player.current:
        time_until += player.current.duration or 0
    embed.add_field(name="Estimated Start", value=fmt_duration(time_until), inline=True)


def create_song_embed(player, song, meta=None, show_autoplay_status=False):
    from_autoplay = song.requester == AUTOPLAY

    embed = discord.Embed(
        title='Track Queued' if not from_autoplay else "Autoplaying Next Song",
        description=song_link(song),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    # IDENTIFIED TRACK / AUTOPLAY TRACK INFO
    if from_autoplay and song.meta:
        embed.add_field(name="Track Info", value=f"`{song.meta}`", inline=False)
    elif meta:
        embed.add_field(name="Identified As", value=f"`{meta}`", inline=False)

    embed.add_field(name="Duration", value=fmt_duration(song.duration), inline=True)
    add_queue_position_fields(embed, player, song)

    # AUTOPLAY STATUS
    if show_autoplay_status and not from_autoplay:
        response_text = "✅ Autoplaying from this track" if meta else "❌ Failed to identify artist/track"
        embed.add_field(name="Autoplay", value=response_text, inline=True)

    embed.set_footer(text=f"Requested by {song.requester}" if not from_autoplay else "Provided by last.fm")
    return embed


def create_playlist_embed(song_count, first_song, duration, seeds):
    embed = discord.Embed(
        title="Playlist Queued",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Tracks Added", value=f"{song_count} songs", inline=True)
    embed.add_field(name="Duration", value=fmt_duration(duration), inline=True)
    embed.add_field(name="First Track", value=song_link(first_song), inline=False)
    autoplay_text = f"✅ Autoplaying from {len(seeds)} identified track{'s' if len(seeds) != 1 else ''}" \
        if seeds else "❌ No songs identified in this playlist"
    embed.add_field(name="Autoplay", value=autoplay_text, inline=False)
    embed.set_footer(text=f"Requested by {first_song.requester}")
    return embed


def create_album_embed(album, requester):
    embed = discord.Embed(
        title="Album Queued",
        description=f"**{album.title}**\n{album.artist}",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Tracks Added", value=f"{len(album.tracks)} songs", inline=True)
    embed.add_field(name="Duration", value=fmt_duration(album.duration), inline=True)
    embed.add_field(name="First Track", value=f"`{album.tracks[0]}`", inline=False)
    embed.add_field(name="Autoplay", value="✅ Autoplaying similar tracks after the album", inline=False)
    embed.set_footer(text=f"Requested by {requester}")
    return embed


### EVENTS ###
@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        return  # the check already replied / not one of ours
    if isinstance(error, commands.UserInputError):
        usage = f"/{ctx.command.qualified_name} {ctx.command.signature}".strip()
        await ctx.send(embed=create_embed("Invalid Command", f"Usage: `{usage}`", discord.Color.red()))
        return
    print(f"[command error] {ctx.command}: {error!r}")
    await ctx.send(embed=create_embed("Error", "Something went wrong running that command.", discord.Color.red()))


### COMMANDS ###
@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')


@bot.command()
@in_voice_channel()
async def disconnect(ctx):
    if ctx.voice_client:
        await get_player(ctx).stop()
        await ctx.voice_client.disconnect()
        await ctx.send("DISCONNECTING! LATER DOOOOOOOOG!")
    else:
        await ctx.send("I'm not in a voice channel!")


@bot.command()
@in_voice_channel()
async def play(ctx, *, search):
    if not await connect_to_voice(ctx):
        return
    player = get_player(ctx)

    if youtube.is_url(search):
        await play_link(ctx, player, search)
    else:
        await play_search(ctx, player, search)


async def find_song(search):
    """Turn typed text into a Song. Returns (song, meta); song is None if nothing was found.

    Identify the song first, then find the right upload of it. If the text
    alone is not enough (no artist, typos), search YouTube with the words as
    typed and identify the song from the video that comes back.
    """
    yt = services.youtube
    meta = await services.identifier.identify_track(search)
    try:
        candidates = await yt.search(f"{meta.artist} - {meta.title}" if meta else search)
    except Exception as e:
        print(f"Error querying YouTube: {e!r}")
        candidates = []
    best = youtube.pick_best(candidates, meta=meta, query=search)
    if not best and candidates:
        best = candidates[0]
    if not best:
        return None, None

    song = Song.from_candidate(best, meta)

    # NOT IDENTIFIED FROM THE TEXT - TRY AGAIN FROM THE VIDEO WE LANDED ON
    if not meta:
        try:
            info = await yt.resolve(song)
            meta = song.meta = await services.identifier.identify_video(info)
        except Exception as e:
            print(f"Could not identify from video: {e!r}")
    return song, meta


async def play_search(ctx, player, search):
    song, meta = await find_song(search)
    if not song:
        await ctx.send(embed=create_embed("Error", "No song found for that request!", discord.Color.red()))
        return
    song.requester = ctx.author.name
    await queue_single(ctx, player, song, meta)


async def play_link(ctx, player, url):
    """/play <url>: a single video or a playlist."""
    try:
        kind, info = await services.youtube.fetch_url(url)
    except Exception as e:
        print(f"Error querying YouTube: {e!r}")
        await ctx.send(embed=create_embed("Error", "No song found for that request!", discord.Color.red()))
        return

    # SINGLE VIDEO
    if kind == "video":
        song = Song.from_info(info)
        song.requester = ctx.author.name
        meta = song.meta = await services.identifier.identify_video(info)
        await queue_single(ctx, player, song, meta)
        return

    # PLAYLIST
    entries = info.get("entries") or []
    if not entries:
        await ctx.send(embed=create_embed("Error", "No song found for that request!", discord.Color.red()))
        return
    songs = []
    for entry in entries:
        song = Song.from_candidate(youtube.Candidate.from_entry(entry))
        song.requester = ctx.author.name
        songs.append(song)
    player.queue.extend(songs)

    # START PLAYBACK WHILE WE WORK OUT WHAT THE PLAYLIST CONTAINS
    _, seeds = await asyncio.gather(player.ensure_playing(), find_playlist_seeds(entries, songs))
    total_duration = sum(s.duration or 0 for s in songs)
    await ctx.send(embed=create_playlist_embed(len(songs), songs[0], total_duration, seeds))
    player.set_seeds(seeds)


async def queue_single(ctx, player, song, meta):
    player.queue.append(song)
    await player.ensure_playing()
    await ctx.send(embed=create_song_embed(player, song, meta, show_autoplay_status=True))

    # AUTOPLAY - rebuilt in the background from this track
    if meta:
        player.set_seeds([meta])


@bot.command()
@in_voice_channel()
async def album(ctx, *, search):
    if not await connect_to_voice(ctx):
        return
    player = get_player(ctx)

    album_meta = await services.identifier.identify_album(search)
    if not album_meta:
        await ctx.send(embed=create_embed("Error", "No album found for that request!", discord.Color.red()))
        return

    # QUEUE EVERY TRACK - each one is matched to YouTube right before it plays
    songs = []
    for track in album_meta.tracks:
        song = Song.from_meta(track)
        song.requester = ctx.author.name
        songs.append(song)
    player.queue.extend(songs)

    await player.ensure_playing()
    await ctx.send(embed=create_album_embed(album_meta, ctx.author.name))

    # AUTOPLAY - similar tracks to a spread of the album, once it ends
    player.set_seeds(random.sample(album_meta.tracks, min(MAX_SEEDS, len(album_meta.tracks))))


@bot.command()
@in_voice_channel()
async def skip(ctx):
    player = get_player(ctx)
    current = player.current
    if not ctx.voice_client or not ctx.voice_client.is_playing() or not current:
        await ctx.send("Nothing is playing!")
        return

    # BUILD EMBED
    embed = create_embed("Track Skipped", f"~~{song_link(current)}~~")

    if player.queue:
        embed.add_field(name="Up Next", value=song_link(player.queue[0]), inline=False)
    elif player.autoplay_queue:
        embed.add_field(name="Up Next (Autoplay)", value=song_link(player.autoplay_queue[0]), inline=False)
        player.suppress_next_announce = True  # this embed already says what's next
    else:
        embed.add_field(name="Up Next", value="Queue is empty", inline=False)

    # SKIP SONG VIA CALLBACK
    ctx.voice_client.stop()
    await ctx.send(embed=embed)


@bot.command()
@in_voice_channel()
async def queue(ctx):
    player = get_player(ctx)
    queue, autoplay_queue, autoplay_recs = player.queue, player.autoplay_queue, player.autoplay_recs

    if not queue and not autoplay_queue and not player.current:
        await ctx.send(embed=create_embed("Invalid Command", "The queue is empty", discord.Color.red()))
        return

    queue_text = ""

    # CURRENTLY PLAYING SONG
    current = player.current
    if current:
        queue_text += f"**Now Playing**\n{song_link(current)} `[{fmt_duration(current.duration)}]`\n"

    # QUEUE
    if queue:
        queue_text += "\n**__Queue__**\n"
    for i, song in enumerate(queue[:QUEUE_DISPLAY_LIMIT], 1):
        queue_text += f"**{i}.** {song_link(song)} `[{fmt_duration(song.duration)}]`\n"
    if len(queue) > QUEUE_DISPLAY_LIMIT:
        queue_text += f"*...and {len(queue) - QUEUE_DISPLAY_LIMIT} more*\n"
    total_duration = sum(s.duration or 0 for s in queue)

    # AUTOPLAY QUEUE
    if autoplay_queue or autoplay_recs:
        queue_text += "\n**__Autoplay Queue (Next 5)__**\n"

        # SONGS ALREADY RETRIEVED FROM YOUTUBE
        for i, song in enumerate(autoplay_queue[:5], 1):
            queue_text += f"**A{i}.** {song_link(song)} `[{fmt_duration(song.duration)}]`\n"

        # UPCOMING RECOMMENDATIONS
        remaining_slots = 5 - len(autoplay_queue)
        if remaining_slots > 0:
            for i, rec in enumerate(autoplay_recs[:remaining_slots], len(autoplay_queue) + 1):
                queue_text += f"**A{i}.** {rec.artist} - {rec.title} `[pending]`\n"

    # FOOTER
    footer_text = f"{len(queue)} songs • Total duration: {fmt_duration(total_duration) if queue else '0:00'}"
    total_autoplay = len(autoplay_queue) + len(autoplay_recs)
    if total_autoplay > 0:
        footer_text += f" • {total_autoplay} autoplay songs available"

    await ctx.send(embed=create_embed('Current Queue', queue_text, discord.Color.purple(), footer_text))


@bot.command()
@in_voice_channel()
async def shuffle(ctx):
    queue = get_player(ctx).queue

    # ERROR HANDLING
    error_text = None
    if not queue:
        error_text = "The queue is empty - there is nothing to shuffle"
    elif len(queue) < 2:
        error_text = "The queue is too small to shuffle"

    if error_text:
        await ctx.send(embed=create_embed("Invalid Command", error_text, discord.Color.red()))
        return

    random.shuffle(queue)

    # CREATE PREVIEW
    preview = "\n".join([f"**{i+1}.** {song.title}" for i, song in enumerate(queue[:5])])
    if len(queue) > 5:
        preview += f"\n*...and {len(queue) - 5} more*"

    await ctx.send(embed=create_embed("Queue Shuffled", preview, discord.Color.blue(), f"Shuffled {len(queue)} songs"))


@bot.command()
@in_voice_channel()
async def clear(ctx):
    queue = get_player(ctx).queue

    if not queue:
        await ctx.send(embed=create_embed("Invalid Command", "The queue is empty - there is nothing to clear", discord.Color.red()))
        return

    song_count = len(queue)
    queue.clear()

    await ctx.send(embed=create_embed("Queue Cleared", f"Removed {song_count} song{'s' if song_count != 1 else ''} from the queue"))


@bot.command()
@in_voice_channel()
async def remove(ctx, position: int):
    queue = get_player(ctx).queue

    # ERROR HANDLING
    error_text = None
    if not queue:
        error_text = "The queue is empty - there is nothing to remove"
    elif position < 1 or position > len(queue):
        error_text = f"Please choose a number between 1 and {len(queue)}"

    if error_text:
        await ctx.send(embed=create_embed("Invalid Command", error_text, discord.Color.red()))
        return

    removed_song = queue.pop(position - 1)

    await ctx.send(embed=create_embed("Song Removed", f"~~{song_link(removed_song)}~~", discord.Color.blue(), f"{len(queue)} song{'s' if len(queue) != 1 else ''} remaining in queue"))


@bot.command()
@in_voice_channel()
async def bump(ctx, position: int):
    queue = get_player(ctx).queue

    # ERROR HANDLING
    error_text = None
    if not queue:
        error_text = "The queue is empty - there is nothing to bump"
    elif position < 2 or position > len(queue):
        error_text = f"Please choose a number between 2 and {len(queue)}"

    if error_text:
        await ctx.send(embed=create_embed("Invalid Command", error_text, discord.Color.red()))
        return

    bumped_song = queue.pop(position - 1)
    queue.insert(0, bumped_song)

    await ctx.send(embed=create_embed("Song Bumped to Top", song_link(bumped_song)))


@bot.command()
@in_voice_channel()
async def autoplay(ctx):
    await ctx.send(embed=create_embed("Autoplay", "Autoplay is now **always enabled** ✅\nThe bot will automatically queue similar songs when the queue runs empty.", discord.Color.purple()))


@bot.command()
async def help(ctx):
    embed = create_embed("BIG CHINA Command List", "Available commands:", color=discord.Color.gold())

    embed.add_field(name="/album <album name>", value="Queue a full album, then autoplay similar tracks", inline=False)
    embed.add_field(name="/bump <position>", value="Move a song to the top of the queue", inline=False)
    embed.add_field(name="/clear", value="Clear the entire queue", inline=False)
    embed.add_field(name="/disconnect", value="Disconnect bot from voice channel", inline=False)
    embed.add_field(name="/play <song/url>", value="Play a song, video or playlist, or add it to queue", inline=False)
    embed.add_field(name="/queue", value="Show the current queue and autoplay queue", inline=False)
    embed.add_field(name="/remove <position>", value="Remove a song at the specified position", inline=False)
    embed.add_field(name="/shuffle", value="Shuffle the queue", inline=False)
    embed.add_field(name="/skip", value="Skip the current song", inline=False)

    embed.set_footer(text="Use / before each command")

    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(TOKEN)
