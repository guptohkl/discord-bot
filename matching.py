"""Text matching: decides whether "what the user typed" is "this track".

Everything here is pure (no network), which is what makes it testable.

The core idea replaces the old whole-string fuzzy ratio with three token
coverage numbers, so word order and the lack of an "artist - title"
separator stop mattering:

    title_cov   how much of the candidate's title appears in the query
    artist_cov  how much of the candidate's artist appears in the query
    query_cov   how much of the query is explained by the candidate
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

STOPWORDS = {"the", "a", "an", "and", "by", "of", "feat", "ft", "featuring"}

# Words people tack onto searches / YouTube titles that say nothing about
# which song it is. Unmatched noise words are ignored rather than penalised.
NOISE = {"official", "video", "audio", "lyrics", "lyric", "hd", "hq", "4k",
         "mv", "visualizer", "visualiser", "explicit", "song"}
# The subset that is safe to delete outright from text sent to a search API
# ("song" is not: Blur's "Song 2" would become "2").
SEARCH_NOISE = NOISE - {"song"}

# A candidate carrying one of these (that the user did not ask for) is a
# different recording, not just a different pressing.
STRONG_VERSION = {"live", "remix", "remixed", "rmx", "acoustic", "instrumental",
                  "karaoke", "cover", "demo", "sped", "slowed", "nightcore",
                  "8d", "tribute", "mashup", "reverb", "unplugged", "clean",
                  "edited", "censored", "commentary", "skit"}
MILD_VERSION = {"remaster", "remastered", "mono", "stereo", "deluxe", "edition",
                "anniversary", "expanded", "bonus", "edit", "radio", "reissue",
                "extended", "version", "mix", "single", "super", "special",
                "collectors", "legacy"}

_BRACKETED = re.compile(r"[\(\[\{【「][^\)\]\}】」]*[\)\]\}】」]")
_FUZZY_TOKEN = 80        # typo tolerance per word
THRESHOLD = 70           # minimum score to call something identified


def normalize(text: str | None) -> str:
    """Lowercase, strip accents and punctuation, and undo stylised spellings."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("$", "s")
    text = re.sub(r"['’`´.!]", "", text)          # don't -> dont, m.A.A.d -> maad
    text = re.sub(r"[^\w\s]|_", " ", text)        # everything else is a space
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str | None) -> list[str]:
    return normalize(text).split()


def content_tokens(text: str | None) -> list[str]:
    """Tokens minus filler words (unless that would leave nothing)."""
    toks = tokens(text)
    kept = [t for t in toks if t not in STOPWORDS]
    return kept or toks


def token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # Short words must match exactly; "love"/"live" style near-misses are
    # different words, "kendrik"/"kendrick" is a typo.
    return max(len(a), len(b)) >= 4 and fuzz.ratio(a, b) >= _FUZZY_TOKEN


def _in(tok: str, haystack: list[str]) -> bool:
    return any(token_match(tok, h) for h in haystack)


def coverage(needles: list[str], haystack: list[str]) -> float:
    """Fraction of `needles` that appear (typo-tolerantly) in `haystack`."""
    if not needles:
        return 0.0
    # Handles spacing differences: "acdc" vs "AC/DC", "RickAstley" vs "Rick Astley".
    joined = "".join(needles)
    if len(joined) >= 4 and joined in "".join(haystack):
        return 1.0
    pairs = [a + b for a, b in zip(haystack, haystack[1:])]     # "ac"+"dc" for "acdc"
    return sum(1 for n in needles if _in(n, haystack) or n in pairs) / len(needles)


def split_title(title: str) -> tuple[str, str]:
    """Split "Creep (Acoustic) - 2011 Remaster" into ("Creep", "Acoustic 2011 Remaster")."""
    extras = " ".join(m.group(0) for m in _BRACKETED.finditer(title or ""))
    core = _BRACKETED.sub(" ", title or "")
    if " - " in core:
        core, _, tail = core.partition(" - ")
        extras += " " + tail
    core = core.strip()
    if not normalize(core):          # title was entirely in brackets
        return (title or "").strip(), ""
    return core, extras.strip()


@dataclass
class MatchScore:
    score: float
    title_cov: float
    artist_cov: float
    query_cov: float
    kind: str = "track"              # "track" or "artist" (artist-only query)
    version_penalty: float = 0.0

    @property
    def strict(self) -> bool:
        """Both the full title and the full artist are present in the query."""
        return self.title_cov >= 0.99 and self.artist_cov >= 0.99


def score_match(query: str, artist: str, title: str) -> MatchScore:
    """Score how well a free-text query describes (artist, title). 0-100."""
    q = content_tokens(query)
    core, extras = split_title(title)
    t = content_tokens(core)
    a = content_tokens(artist)
    x = tokens(extras)

    title_cov = coverage(t, q)
    artist_cov = coverage(a, q)
    matchable = t + a + x
    counted = [tok for tok in q if tok not in NOISE or _in(tok, matchable)]
    query_cov = coverage(counted, matchable) if counted else 1.0

    score = 100 * (0.45 * query_cov + 0.40 * title_cov + 0.15 * artist_cov)
    kind = "track"
    if artist_cov >= 0.99 and query_cov >= 0.99 and title_cov < 0.99:
        # The user typed only an artist (or artist + part of a title).
        kind = "artist"
        score = max(score, 83.0)

    # Penalise versions the user did not ask for. Words in the core title
    # ("Live Forever", "Cover Me Up") are part of the name, not a version.
    penalty = 0.0
    strong = {tok for tok in x if tok in STRONG_VERSION and not _in(tok, q)}
    mild = {tok for tok in x if tok in MILD_VERSION and not _in(tok, q)}
    penalty += min(30, 20 * len(strong)) + min(6, 3 * len(mild))

    return MatchScore(score - penalty, title_cov, artist_cov, query_cov, kind, penalty)


def rank(query: str, items: list, *, artist=lambda i: i.artist, title=lambda i: i.title,
         adjust=None) -> list[tuple[float, MatchScore, object]]:
    """Score every item, best first.

    Earlier items get a small edge: the provider's own relevance/popularity
    order is the tie-breaker when the text alone is ambiguous ("creep").
    """
    ranked = []
    for idx, item in enumerate(items):
        ms = score_match(query, artist(item), title(item))
        final = ms.score + (adjust(item, ms, idx) if adjust else position_penalty(idx))
        ranked.append((final, ms, item))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return ranked


def position_penalty(idx: int) -> float:
    return -min(idx, 8) * 1.0


def shortlist(query: str, items: list, *, threshold: float = THRESHOLD, accept=None, **kw):
    """Every acceptable (final, match_score, item) at or above the threshold, best first."""
    return [r for r in rank(query, items, **kw)
            if r[0] >= threshold and (accept is None or accept(r[1], r[2]))]


def best_match(query: str, items: list, *, threshold: float = THRESHOLD, accept=None, **kw):
    """Best item scoring at or above the threshold, else None.

    `accept(match_score, item)` can veto individual items.
    """
    found = shortlist(query, items, threshold=threshold, accept=accept, **kw)
    return found[0][2] if found else None


def track_key(artist: str | None, title: str | None) -> str:
    """Stable identity for de-duplication ("have we already played this?")."""
    core, _ = split_title(title or "")
    return f"{normalize(artist)}|{normalize(core)}"


def duration_plausible(video: int | None, track: int | None) -> bool:
    """Could a video of this length be this track? (Unknown = yes.)"""
    if not video or not track:
        return True
    return 0.6 * track - 5 <= video <= 1.6 * track + 90


# ---------------------------------------------------------------------------
# Cleaning text before it is sent to a metadata search API
# ---------------------------------------------------------------------------
_KEEP_IN_BRACKETS = {"remix", "live", "acoustic", "remixed", "unplugged", "instrumental"}
_FEAT = re.compile(r"\b(feat|ft|featuring)\b\.?\s+[^-–—|()\[\]]+", re.IGNORECASE)
_SEPARATORS = re.compile(r"[-–—|:~•·/\\\"“”]+")


def search_text(query: str) -> str:
    """Strip noise words so strict search APIs still return hits.

    Deezer wants every term to match something, so "creep radiohead lyrics"
    would otherwise find nothing.
    """
    words = [w for w in re.sub(r"[()\[\]{}]", " ", _SEPARATORS.sub(" ", query)).split()
             if normalize(w) not in SEARCH_NOISE and normalize(w)]
    return " ".join(words) or query.strip()


def clean_video_title(title: str) -> str:
    """Turn a YouTube title into something searchable.

    "Rick Astley - Never Gonna Give You Up (Official Video) [4K Remaster]"
    -> "Rick Astley Never Gonna Give You Up"
    """
    def keep_or_drop(m):
        inner = normalize(m.group(0))
        return f" {inner} " if set(inner.split()) & _KEEP_IN_BRACKETS else " "

    text = _BRACKETED.sub(keep_or_drop, title or "")
    text = _FEAT.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    words = [w for w in text.split() if normalize(w) and normalize(w) not in SEARCH_NOISE]
    return " ".join(words)


def clean_channel(channel: str | None) -> str:
    """ "Rick Astley - Topic" / "RickAstleyVEVO" -> the artist-ish part."""
    text = re.sub(r"\s*-\s*Topic$", "", channel or "", flags=re.IGNORECASE)
    text = re.sub(r"(?i)vevo$", "", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)           # RickAstley -> Rick Astley
    text = re.sub(r"(?i)\b(official|music|records|tv|channel)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()
