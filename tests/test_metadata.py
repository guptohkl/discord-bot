"""Song / album / video recognition against the fake Deezer + iTunes."""
import pytest

from conftest import VIDEOS


def video(vid):
    return next(x for x in VIDEOS if x["id"] == vid)


# ---------------------------------------------------------------- /play text
@pytest.mark.parametrize("query, artist, title", [
    ("radiohead creep", "Radiohead", "Creep"),
    ("creep radiohead", "Radiohead", "Creep"),
    ("Radiohead - Creep", "Radiohead", "Creep"),
    ("tlc creep", "TLC", "Creep"),
    ("creep radiohead lyrics", "Radiohead", "Creep"),
    ("kendrick humble", "Kendrick Lamar", "HUMBLE."),
    ("humble kendrick lamar official audio", "Kendrick Lamar", "HUMBLE."),
    ("kendrick lamar money trees", "Kendrick Lamar", "Money Trees (feat. Jay Rock)"),
    ("kendrik lamar money trees", "Kendrick Lamar", "Money Trees (feat. Jay Rock)"),   # typo
    ("radiohead creep pablo honey 1993", "Radiohead", "Creep"),                        # stray word
    ("asap rocky praise the lord", "A$AP Rocky", "Praise The Lord (Da Shine) (feat. Skepta)"),
    ("acdc thunderstruck", "AC/DC", "Thunderstruck"),
    ("queen bohemian rhapsody", "Queen", "Bohemian Rhapsody"),   # not the Live Aid cut
    ("beyonce halo", "Beyoncé", "Halo"),
    ("oasis live forever", "Oasis", "Live Forever"),         # "live" is the title here
    ("radiohead creep acoustic", "Radiohead", "Creep (Acoustic)"),
])
async def test_identify_track(identifier, query, artist, title):
    meta = await identifier.identify_track(query)
    assert meta is not None, query
    assert (meta.artist, meta.title) == (artist, title)
    assert meta.duration


async def test_artist_only_query_gives_that_artists_top_track(identifier):
    meta = await identifier.identify_track("madonna")
    assert meta.artist == "Madonna"          # not the song called "Madonna" by Some Band


@pytest.mark.parametrize("query", ["asdkjhasd qwpoeiu", "lofi beats to study to", "minecraft speedrun world record"])
async def test_unknown_text_is_not_identified(identifier, query):
    assert await identifier.identify_track(query) is None


@pytest.mark.parametrize("query", ["creep", "mr brightside", "halo", "rain sounds"])
async def test_title_without_artist_is_not_settled_from_the_catalog(identifier, query):
    # Seen live: Deezer lists covers first for artist-less titles. These go via YouTube instead
    # (covered in test_bot_commands).
    assert await identifier.identify_track(query) is None


async def test_title_only_album_picks_the_one_with_the_best_known_tracks(identifier):
    # Seen live: "dark side of the moon" matched another artist's album of the same name.
    album = await identifier.identify_album("ok computer")
    assert album.artist == "Radiohead"
    # ...but naming the artist always wins
    assert (await identifier.identify_album("obscure act ok computer")).artist == "Obscure Act"


async def test_falls_back_to_itunes_when_deezer_is_down(identifier, http):
    http.deezer_down = True
    meta = await identifier.identify_track("radiohead creep")
    assert (meta.artist, meta.title, meta.source) == ("Radiohead", "Creep", "itunes")
    assert meta.duration == 238


async def test_falls_back_to_itunes_when_deezer_finds_nothing(identifier, http):
    http.deezer_empty = True
    assert (await identifier.identify_track("kendrick humble")).source == "itunes"


# -------------------------------------------------------------------- /album
@pytest.mark.parametrize("query, title, count", [
    ("ok computer", "OK Computer", 3),                       # standard, not the deluxe
    ("radiohead ok computer", "OK Computer", 3),
    ("good kid maad city", "good kid, m.A.A.d city", 3),
    ("good kid mad city kendrick", "good kid, m.A.A.d city", 3),
    ("good kid maad city deluxe", "good kid, m.A.A.d city (Deluxe)", 4),
    ("pablo honey", "Pablo Honey", 1),
])
async def test_identify_album(identifier, query, title, count):
    album = await identifier.identify_album(query)
    assert album is not None, query
    assert album.title == title and len(album.tracks) == count


async def test_album_tracks_are_in_track_order_with_durations(identifier):
    album = await identifier.identify_album("ok computer")
    assert [t.title for t in album.tracks] == ["Karma Police", "No Surprises", "Paranoid Android"]
    assert all(t.duration and t.artist == "Radiohead" for t in album.tracks)
    assert album.duration == 264 + 229 + 387


async def test_album_prefers_album_over_single_of_same_name(identifier):
    # "Creep" exists only as a single here; a real album search should still resolve
    album = await identifier.identify_album("radiohead creep")
    assert album.title == "Creep" and len(album.tracks) == 1


async def test_unknown_album(identifier):
    assert await identifier.identify_album("zzzz not an album qqqq") is None


async def test_album_via_itunes_fallback(identifier, http):
    http.deezer_down = True
    album = await identifier.identify_album("radiohead ok computer")
    assert album.source == "itunes" and len(album.tracks) == 3


# -------------------------------------------------- YouTube video -> track
async def test_video_with_youtube_music_tags(identifier):
    meta = await identifier.identify_video(video("creep_topic"))
    assert (meta.artist, meta.title) == ("Radiohead", "Creep")


async def test_video_with_tags_unknown_to_catalog_still_identified(identifier):
    info = {"title": "Obscure B-Side", "track": "Obscure B-Side", "artist": "Tiny Band, Friend",
            "duration": 200}
    meta = await identifier.identify_video(info)
    assert (meta.artist, meta.title, meta.source) == ("Tiny Band", "Obscure B-Side", "youtube")


async def test_video_identified_from_artist_dash_title(identifier):
    meta = await identifier.identify_video(video("rick"))
    assert (meta.artist, meta.title) == ("Rick Astley", "Never Gonna Give You Up")


async def test_video_identified_from_channel_plus_title(identifier):
    meta = await identifier.identify_video(video("rick_notags"))
    assert (meta.artist, meta.title) == ("Rick Astley", "Never Gonna Give You Up")


async def test_flat_playlist_entry_is_enough_to_identify(identifier):
    flat = {"title": "The Killers - Mr. Brightside (Official Music Video)",
            "channel": "TheKillersVEVO", "duration": 228}
    assert (await identifier.identify_video(flat)).title == "Mr. Brightside"


@pytest.mark.parametrize("vid", ["zoo", "lofi", "podcast", "okc_full"])
async def test_non_song_videos_are_rejected(identifier, vid):
    assert await identifier.identify_video(video(vid)) is None


async def test_wrong_length_video_is_rejected(identifier):
    # Title says Creep, but a 50 minute video is not the 4 minute song.
    info = {"title": "Radiohead - Creep", "channel": "x", "duration": 3000}
    assert await identifier.identify_video(info) is None
    info["duration"] = 14 * 60          # under the hard cap, still implausible
    assert await identifier.identify_video(info) is None


# ------------------------------------------------- mixtapes (MusicBrainz)
async def test_mixtape_missing_from_streaming_catalogs_is_found_on_musicbrainz(identifier):
    album = await identifier.identify_album("action bronson - blue chips")
    assert album.source == "musicbrainz"
    # "Blue Chips", not "Blue Chips 7000" (listed first) or the "1 & 2" compilation
    assert (album.artist, album.title) == ("Action Bronson & Party Supplies", "Blue Chips")
    assert [t.title for t in album.tracks] == ["Pouches of Tuna", "Steve Wynn", "Hookers at the Point"]
    assert [t.duration for t in album.tracks] == [203, 152, 251]      # ms -> s, bonus pressing ignored


async def test_longer_title_still_reachable(identifier):
    album = await identifier.identify_album("action bronson blue chips 7000")
    assert (album.title, album.source) == ("Blue Chips 7000", "deezer")


async def test_partial_album_title_is_used_when_nothing_matches_fully(identifier, http):
    album = await identifier.identify_album("kendrick good kid")
    assert album.title == "good kid, m.A.A.d city"
    assert [u for u, _ in http.calls if "musicbrainz" in u]      # everyone was asked first


async def test_musicbrainz_is_not_asked_when_a_streaming_catalog_has_the_album(identifier, http):
    await identifier.identify_album("radiohead ok computer")
    assert not [u for u, _ in http.calls if "musicbrainz" in u]


async def test_musicbrainz_is_never_asked_about_single_tracks(identifier, http):
    await identifier.identify_track("asdkjh qwlekj")
    await identifier.identify_video({"title": "Some - Video", "channel": "x", "duration": 100})
    assert not [u for u, _ in http.calls if "musicbrainz" in u]


async def test_musicbrainz_requests_are_spaced_out(http):
    import time
    import metadata
    mb = metadata.MusicBrainz(http)
    mb.MIN_INTERVAL = 0.05
    start = time.monotonic()
    await mb.search_albums("action bronson blue chips")
    await mb.album_tracks((await mb.search_albums("action bronson blue chips"))[0])
    assert time.monotonic() - start >= 0.1


async def test_musicbrainz_503_is_retried(identifier, http):
    # Seen live: MusicBrainz answers 503 at random when the service is busy.
    http.musicbrainz_503s = 2
    album = await identifier.identify_album("action bronson - blue chips")
    assert album and album.source == "musicbrainz" and len(album.tracks) == 3


async def test_musicbrainz_gives_up_after_repeated_503s(identifier, http):
    http.musicbrainz_503s = 99
    # With the mixtape source down, the closest thing a working catalog has is used.
    album = await identifier.identify_album("action bronson - blue chips")
    assert (album.title, album.source) == ("Blue Chips 7000", "deezer")
    assert [t.title for t in album.tracks] == ["Wolfpack"]
    assert len([u for u, _ in http.calls if "musicbrainz" in u]) == 4      # 1 try + 3 retries
