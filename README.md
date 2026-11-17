# Discord music bot: backend overhaul

Commands are unchanged, plus one new one (`/album`). Everything behind them was rebuilt.

## Files

| File | Job |
|---|---|
| `bot.py` | Discord commands and embeds only. Thin. |
| `player.py` | Per-server state machine: queue, autoplay queue, what plays next. |
| `metadata.py` | Song / album / video recognition (Deezer, iTunes fallback, MusicBrainz for mixtapes). Replaces `musicbrainz.py`. |
| `matching.py` | The text-matching rules. Pure functions, no network. |
| `youtube.py` | yt-dlp calls and the "which upload do we play" scoring. |
| `lastfm.py` | Recommendations (now async). |
| `models.py` | `Song`, `TrackMeta`, etc. |
| `diagnose.py` | Command-line tool: shows what the bot would do for an input. |
| `tests/` | 181 offline tests. `tests/live/` holds 38 checks against the real services. |

Delete `musicbrainz.py` (the old client). The `MUSICBRAINZ_*` variables in `.env` are still used: they identify the bot to MusicBrainz, which is now the mixtape fallback for `/album`.

## What changed, by request

### 1. Song recognition

**Problem.** MusicBrainz has to be told which words are the artist and which are the title. The old code guessed the artist from the whole string, then fuzzy-compared the whole string against `artist - track`, so word order, a missing artist, or extra words all hurt.

**Fix.** Recognition now uses Deezer's search (no API key), with iTunes as a fallback if Deezer is down or finds nothing. Both take one free-text box and return popularity-ranked tracks *with durations*. No separator rule: `creep radiohead`, `Radiohead - Creep`, `creep by radiohead lyrics` are the same query.

The match is scored on three numbers instead of one string ratio (`matching.py`):

- how much of the candidate's **title** is in what you typed
- how much of its **artist** is in what you typed
- how much of **what you typed** is explained by the candidate

On top of that:

- Typos are tolerated per word (`kendrik` = `kendrick`); short words must match exactly.
- Stylised names are normalised (`A$AP` = `asap`, `AC/DC` = `acdc`, `m.A.A.d` = `maad`, accents stripped).
- Words like `lyrics`, `official audio`, `hd` are ignored.
- Live / remix / karaoke / acoustic / clean versions lose unless you asked for them. "Live Forever" is not treated as a live version.
- A title with no artist (`mr brightside`) is not settled from the catalog, because every recording of that title ties and the catalog lists covers first. It is searched on YouTube as typed, whose top result is the famous version, and identified from that video. Typing the artist is the reliable path.
- If the search finds nothing, it retries with each word left out in turn, which rescues single typos and stray words.

**Safety net.** If the text still is not identified, the bot searches YouTube with your words as before, then identifies the song from the video it lands on (see 4). So a miss costs a second, not autoplay.

### 2. `/album <name>`

Include the artist (`/album pink floyd dark side of the moon`). A bare title can match a different artist's album of the same name.

**Mixtapes.** Free mixtapes are mostly missing from Deezer and iTunes, so `/album` falls back to MusicBrainz, which has them with track lengths. It is asked last because it is slower and limited to one request per second, so a mixtape takes a few seconds longer to queue. An album whose whole title you typed always beats a partial match from an earlier source (`blue chips` gets the mixtape, not *Blue Chips 7000*).

Deezer gives the album's real tracklist with each track's length. The bot queues every track as its own entry and matches each one to YouTube individually, right before it plays.

This deliberately does **not** look for a YouTube playlist. Playlists for albums are unreliable (wrong order, missing tracks, music videos mixed in); a tracklist plus per-track matching guarantees the right songs in the right order. When two editions match, the standard one wins over deluxe unless you type "deluxe".

### 3. Picking the right upload

The old filter excluded titles by keyword and took the first survivor. Now every search result is scored (`youtube.py`), and because the track is known, **its real duration is the strongest signal**: the upload within a couple of seconds of the album length is the album audio. A music video with a skit, a live cut, or a full-album upload is the wrong length. Auto-generated "Artist - Topic" uploads and "official audio" get a bonus; cover / live / remix / clean / sped up / karaoke / reaction get a penalty unless requested.

### 4. Autoplay from a YouTube link or playlist

For a link, the bot tries in order:

1. YouTube's own music tags on the video (track / artist), present on most label uploads.
2. The video title (and channel name), but only if a catalog lookup confirms it under strict rules: the whole artist and whole title must appear, most of the title must be explained, and the video length must be plausible for the track.

Videos over 15 minutes, non-music categories with weak matches, and anything unconfirmed are treated as "not a song": they play, with no autoplay. For playlists, the first 8 entries are checked and up to 3 identified songs seed autoplay.

### 5. Autoplay / Last.fm

- Lookups go by artist + title with Last.fm autocorrect. The old code refused to autoplay without a MusicBrainz ID, and those IDs rarely matched Last.fm anyway.
- Because recognition now succeeds far more often, the weaker similar-artist fallback is hit less. It is still there.
- Albums and playlists seed from up to 3 tracks, interleaved.
- Autoplay never repeats a song already played this session or sitting in the queue.
- When the 25 recommendations run out, it reseeds from the last song played instead of going silent.

## Other fixes

- **Expired stream URLs.** Stream URLs are fetched right before a song plays (and the next song is prefetched), not at queue time. Long queues and playlists no longer die hours in.
- **Playlists queue quickly.** Entries are listed without fully extracting each video (the old code extracted every one up front). Capped at 200 entries.
- **Nothing blocks the event loop.** Last.fm and recognition calls are async; the old `requests` calls froze the bot while waiting.
- **`/shuffle`, `/remove`, `/bump` error messages** had a trailing comma that turned the text into a tuple.
- **`/skip` into autoplay** no longer posts the next song twice.
- **`/queue`** caps the list at 15 so big queues do not exceed Discord's embed limit.
- **Unplayable videos** are skipped with a message instead of stalling the queue.
- **Wrong arguments** (`/remove abc`) get a usage message.
- `/disconnect` also clears autoplay state so nothing restarts afterwards.

## Behaviour differences to know about

- Autoplay loads in the background after the "Track Queued" embed, so `/play` replies sooner.
- Queued album and autoplay tracks show as `Artist - Title` (no link) until they are about to play.
- Any `http(s)` link is handed to yt-dlp, not just YouTube ones.

## Deploying

From the folder with `docker-compose.yml`:

```
docker compose build
```
Rebuilds the image. Needed because the Python dependencies changed (MusicBrainz and fuzzywuzzy removed, pytest added) and there are new files to copy in.

```
docker compose run --rm discord-music-bot python -m pytest
```
Runs the offline tests inside the new image, in a throwaway container (`--rm` deletes it afterwards). The bot is not started. Expect `181 passed, 38 skipped`.

```
docker compose run --rm discord-music-bot python -m pytest --live tests/live -v
```
Runs the live checks from the server's own network (see below). Takes a minute or two.

```
docker compose up -d
```
Starts the bot.

## Tests

**Offline (`python -m pytest`)** - 181 tests, about a second, no network, no token. Deezer, iTunes, Last.fm, YouTube and the Discord voice connection are replaced with fakes that answer in the real response shapes. Covers every command end to end, the matching rules, autoplay, album playback order, stream refresh, failure handling and the fallbacks.

**Live (`--live tests/live`)** - 38 checks against the real services: recognition of 13 messy song queries and 5 albums, each provider on its own, Last.fm, YouTube search shape, that the picked video is within 15 seconds of the real track length, link and playlist handling, and decoding 3 seconds of a real stream through ffmpeg. I could not run these from where the code was written (no access to those services), so **this is the run that matters**. If any fail, send me the output.

**Diagnose** - when the bot picks something odd:

```
docker compose run --rm discord-music-bot python diagnose.py play "creep radiohead"
docker compose run --rm discord-music-bot python diagnose.py album "good kid maad city"
docker compose run --rm discord-music-bot python diagnose.py link "https://www.youtube.com/watch?v=..."
```
Prints the identification, every YouTube candidate with its score, the pick, and the recommendations.

**In Discord** - the one part that cannot be automated (real voice):

1. `/play creep radiohead` - plays, embed says *Identified As Radiohead - Creep*, Autoplay ✅.
2. `/queue` - shows Now Playing and an Autoplay Queue.
3. `/skip` - one "Track Skipped" embed, autoplay song starts, no duplicate embed.
4. `/album ok computer` - "Album Queued", 12 tracks; `/queue` shows them in order; `/skip` a few times and confirm each is the right track.
5. `/play <YouTube link to a song>` - Identified, Autoplay ✅. `/play <link to a non-music video>` - plays, Autoplay ❌.
6. `/play <playlist link>` - "Playlist Queued" with an Autoplay line.
7. `/shuffle`, `/bump 2`, `/remove 1`, `/clear`, then `/disconnect`.

## Tuning knobs

| Constant | File | Meaning |
|---|---|---|
| `THRESHOLD` (70) | `matching.py` | Minimum score to call text identified. |
| `MAX_SONG_SECONDS` (900) | `metadata.py` | Videos longer than this are never treated as a song. |
| `AUTOPLAY_BUFFER` (2) | `player.py` | Autoplay songs looked up ahead of time. |
| `STREAM_MAX_AGE` (3 h) | `models.py` | Age at which a stream URL is refetched. |
