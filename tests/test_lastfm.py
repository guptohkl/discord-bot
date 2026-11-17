"""Last.fm recommendations against the fake API."""
import matching
from models import TrackMeta


def names(recs):
    return [(r.artist, r.title) for r in recs]


async def test_similar_tracks_by_name(services):
    recs = await services.lastfm.recommendations([TrackMeta("Radiohead", "Creep")])
    assert ("Radiohead", "Karma Police") in names(recs) and len(recs) == 4


async def test_no_mbid_needed_and_autocorrect_requested(services, http):
    await services.lastfm.recommendations([TrackMeta("Radiohead", "Creep")])
    _, params = http.calls[-1]
    assert params["method"] == "track.getsimilar" and "mbid" not in params
    assert params["autocorrect"] == 1 and params["api_key"] == "test"


async def test_falls_back_to_similar_artists(services):
    # No similar-track data for HUMBLE. -> top tracks of Kendrick + similar artists
    recs = await services.lastfm.recommendations([TrackMeta("Kendrick Lamar", "HUMBLE.")])
    artists = {r.artist for r in recs}
    assert artists == {"Kendrick Lamar", "Radiohead"}
    assert ("Kendrick Lamar", "HUMBLE.") not in names(recs)      # never recommends the seed


async def test_multiple_seeds_are_interleaved_and_deduped(services):
    seeds = [TrackMeta("Radiohead", "Karma Police"), TrackMeta("Radiohead", "No Surprises")]
    recs = names(await services.lastfm.recommendations(seeds))
    assert recs[:2] == [("Radiohead", "Creep"), ("Radiohead", "Paranoid Android")]
    assert recs.count(("Radiohead", "Creep")) == 1
    assert ("Tame Impala", "The Less I Know The Better") in recs
    assert not {("Radiohead", "Karma Police"), ("Radiohead", "No Surprises")} & set(recs)


async def test_exclude_removes_already_played(services):
    exclude = {matching.track_key("Radiohead", "Karma Police")}
    recs = names(await services.lastfm.recommendations([TrackMeta("Radiohead", "Creep")], exclude=exclude))
    assert ("Radiohead", "Karma Police") not in recs and len(recs) == 3


async def test_unknown_everything_returns_empty(services):
    assert await services.lastfm.recommendations([TrackMeta("Nobody", "Nothing")]) == []


async def test_api_failure_returns_empty(services, http):
    async def boom(*a, **k):
        raise ConnectionError("down")
    http.get_json = boom
    assert await services.lastfm.recommendations([TrackMeta("Radiohead", "Creep")]) == []


async def test_single_result_object_instead_of_list(services, http):
    async def one(url, params=None, timeout=8):
        return {"similartracks": {"track": {"name": "Only One", "artist": {"name": "Solo"}}}}
    http.get_json = one
    assert names(await services.lastfm.similar_tracks("a", "b")) == [("Solo", "Only One")]
