"""Text normalisation and scoring (pure functions)."""
import pytest

import matching
from matching import score_match


@pytest.mark.parametrize("raw, expected", [
    ("HUMBLE.", "humble"),
    ("good kid, m.A.A.d city", "good kid maad city"),
    ("A$AP Rocky", "asap rocky"),
    ("Beyoncé", "beyonce"),
    ("Don't Stop Me Now", "dont stop me now"),
    ("AC/DC", "ac dc"),
    ("Simon & Garfunkel", "simon and garfunkel"),
    ("  Mr.   Brightside ", "mr brightside"),
])
def test_normalize(raw, expected):
    assert matching.normalize(raw) == expected


def test_typos_match_but_different_words_do_not():
    assert matching.token_match("kendrik", "kendrick")
    assert matching.token_match("tres", "trees")
    assert not matching.token_match("love", "live")
    assert not matching.token_match("in", "on")


def test_split_title():
    assert matching.split_title("Creep (Acoustic)") == ("Creep", "(Acoustic)")
    assert matching.split_title("Song - 2011 Remaster")[0] == "Song"
    assert matching.split_title("(I Can't Get No) Satisfaction")[0] == "Satisfaction"
    assert matching.split_title("(What's the Story)")[0] == "(What's the Story)"


@pytest.mark.parametrize("query", [
    "radiohead creep", "creep radiohead", "Radiohead - Creep", "creep by radiohead",
    "RADIOHEAD   CREEP", "radiohed creep", "radiohead creep lyrics", "radiohead creep official audio",
])
def test_word_order_separators_typos_and_noise_do_not_matter(query):
    ms = score_match(query, "Radiohead", "Creep")
    assert ms.score >= 95, (query, ms)


def test_title_only_query_is_accepted_but_ranks_below_artist_plus_title():
    title_only = score_match("creep", "Radiohead", "Creep")
    both = score_match("radiohead creep", "Radiohead", "Creep")
    assert matching.THRESHOLD <= title_only.score < both.score


def test_unrelated_text_is_rejected():
    assert score_match("lofi hip hop beats to study to", "Lofi Girl", "Snowman").score < matching.THRESHOLD
    assert score_match("radiohead creep", "Kendrick Lamar", "HUMBLE.").score < matching.THRESHOLD
    assert score_match("creep tlc", "Radiohead", "Creep").score < matching.THRESHOLD


def test_unrequested_versions_are_penalised_and_requested_ones_win():
    plain = score_match("radiohead creep", "Radiohead", "Creep")
    acoustic = score_match("radiohead creep", "Radiohead", "Creep (Acoustic)")
    assert plain.score > acoustic.score
    assert (score_match("radiohead creep acoustic", "Radiohead", "Creep (Acoustic)").score
            > score_match("radiohead creep acoustic", "Radiohead", "Creep").score)


def test_version_word_inside_the_real_title_is_not_a_penalty():
    assert score_match("oasis live forever", "Oasis", "Live Forever").version_penalty == 0


def test_stylised_names():
    assert score_match("asap rocky praise the lord", "A$AP Rocky",
                       "Praise The Lord (Da Shine) (feat. Skepta)").score >= 95
    assert score_match("acdc thunderstruck", "AC/DC", "Thunderstruck").score >= 95
    assert score_match("kendrick mad city", "Kendrick Lamar", "m.A.A.d city").score >= 85
    assert score_match("beyonce halo", "Beyoncé", "Halo").score >= 95


def test_artist_only_query_is_flagged():
    ms = score_match("radiohead", "Radiohead", "Karma Police")
    assert ms.kind == "artist" and ms.score >= matching.THRESHOLD


def test_strict_requires_full_artist_and_title():
    assert score_match("rick astley never gonna give you up", "Rick Astley", "Never Gonna Give You Up").strict
    assert not score_match("never gonna give you up", "Rick Astley", "Never Gonna Give You Up").strict


def test_track_key_ignores_version_suffixes_and_case():
    assert matching.track_key("Radiohead", "Creep (Remastered)") == matching.track_key("radiohead", "creep")
    assert matching.track_key("Radiohead", "Creep") != matching.track_key("TLC", "Creep")


def test_duration_plausible():
    assert matching.duration_plausible(242, 238)
    assert matching.duration_plausible(None, 238)
    assert not matching.duration_plausible(3200, 238)     # full album
    assert not matching.duration_plausible(19, 238)


@pytest.mark.parametrize("title, expected", [
    ("Rick Astley - Never Gonna Give You Up (Official Video) [4K Remaster]", "Rick Astley Never Gonna Give You Up"),
    ("Kendrick Lamar - HUMBLE.", "Kendrick Lamar HUMBLE."),
    ("Artist ft. Other Guy - Song Name (Official Audio)", "Artist Song Name"),
    ("Song Name (Lyrics)", "Song Name"),
    ("Artist - Song (Skrillex Remix)", "Artist Song skrillex remix"),
])
def test_clean_video_title(title, expected):
    assert matching.clean_video_title(title) == expected


def test_clean_channel():
    assert matching.clean_channel("Radiohead - Topic") == "Radiohead"
    assert matching.clean_channel("RickAstleyVEVO") == "Rick Astley"
    assert matching.clean_channel("Queen Official") == "Queen"


def test_search_text_strips_noise_but_never_returns_empty():
    assert matching.search_text("creep radiohead lyrics") == "creep radiohead"
    assert matching.search_text("Radiohead - Creep (official video)") == "Radiohead Creep"
    assert matching.search_text("audio") == "audio"
