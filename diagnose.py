"""Show what the bot would do for an input, without Discord.

    python diagnose.py play  "creep radiohead"
    python diagnose.py album "good kid maad city"
    python diagnose.py link  "https://www.youtube.com/watch?v=..."
    python diagnose.py raw   "dark side of the moon"     (what the catalogs return, scored)

Prints the identification, every YouTube candidate with its score, the pick,
and the first Last.fm recommendations. Paste the output when something picks
the wrong song.
"""
import asyncio
import sys

from dotenv import load_dotenv

import lastfm
import metadata
import youtube
from models import Candidate


def show_candidates(cands, meta=None, query=None):
    best = youtube.pick_best(cands, meta=meta, query=query)
    for i, c in enumerate(cands):
        score = youtube.score_candidate(c, i, meta=meta, query=query)
        mark = "->" if c is best else "  "
        print(f"  {mark} {score:7.1f}  [{c.duration}s]  {c.title}  |  {c.channel}")
    return best


async def show_recs(lf, seeds):
    recs = await lf.recommendations(seeds)
    print(f"\nLast.fm recommendations ({len(recs)}):")
    for r in recs[:10]:
        print(f"     {r}")


async def main(mode, text):
    http = metadata.Http()
    ident = metadata.Identifier([metadata.Deezer(http), metadata.ITunes(http),
                                   metadata.MusicBrainz(http)])
    yt, lf = youtube.YouTube(), lastfm.LastFMClient(http)
    try:
        if mode == "play":
            meta = await ident.identify_track(text)
            print(f"\nIdentified: {meta} [{meta.duration}s, {meta.source}, popularity {meta.popularity}]"
                  if meta else "\nIdentified: nothing")
            query = f"{meta.artist} - {meta.title}" if meta else text
            print(f'\nYouTube candidates for "{query}":')
            show_candidates(await yt.search(query), meta=meta, query=text)
            if meta:
                await show_recs(lf, [meta])
        elif mode == "album":
            album = await ident.identify_album(text)
            if not album:
                print("\nAlbum: nothing")
                return
            print(f"\nAlbum: {album.artist} - {album.title} ({len(album.tracks)} tracks, {album.source})")
            for n, track in enumerate(album.tracks, 1):
                cand = await yt.find(track)
                found = f"{cand.title} | {cand.channel} [{cand.duration}s]" if cand else "NOT FOUND"
                print(f"  {n:2}. {track.title} [{track.duration}s]  ->  {found}")
            await show_recs(lf, album.tracks[:3])
        elif mode == "link":
            kind, info = await yt.fetch_url(text)
            print(f"\nKind: {kind}")
            if kind == "video":
                for key in ("title", "channel", "duration", "track", "artist", "artists", "album", "categories"):
                    print(f"  {key}: {info.get(key)!r}")
                meta = await ident.identify_video(info)
                print(f"\nIdentified: {meta}" if meta else "\nIdentified: nothing (no autoplay)")
                if meta:
                    await show_recs(lf, [meta])
            else:
                for entry in info["entries"][:15]:
                    c = Candidate.from_entry(entry)
                    meta = await ident.identify_video(entry)
                    print(f"  [{c.duration}s] {c.title} | {c.channel}  ->  {meta or 'not a song'}")
        elif mode == "raw":
            import matching
            for provider in ident.providers:
                text = matching.search_text(text)
                for label, search, adjust in (("tracks", provider.search_tracks, metadata._track_adjust),
                                              ("albums", provider.search_albums, metadata._album_adjust)):
                    print(f"\n{provider.name} {label}:")
                    items = await search(text)
                    order = {id(item): i for i, item in enumerate(items)}
                    for final, ms, item in matching.rank(text, items, adjust=adjust):
                        extra = getattr(item, "popularity", None) or getattr(item, "record_type", "")
                        print(f"  {final:6.1f}  #{order[id(item)]:<2} {item.artist} - {item.title}  [{extra}]")
        else:
            print(__doc__)
    finally:
        await http.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:])))
