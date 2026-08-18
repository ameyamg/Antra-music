"""
String similarity helpers for track matching.
"""
import re
from difflib import SequenceMatcher
from typing import Optional


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    text = text.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)   # Remove parenthetical suffixes
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Matches collaboration credits that catalog search engines don't index:
# "(with X)", "[with X]", "(feat. X)", "[feat. X]", "(ft. X)", "(featuring X)", etc.
_COLLAB_CREDIT_RE = re.compile(
    r"\s*[\(\[](with|feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]",
    re.IGNORECASE,
)


def strip_collab(title: str) -> str:
    """Remove collaboration credits from a track title before sending to catalog
    search APIs. Catalogs index tracks under the clean title only — including
    '(with Travis Scott)' or '[feat. Future]' breaks text search.

    Used for search query construction only; raw title is still used for
    similarity scoring so the match is validated against the full title.
    """
    return _COLLAB_CREDIT_RE.sub("", title).strip()


def string_similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity between two strings after normalization."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ── Version / recording-class markers ────────────────────────────────────────
# normalize() strips ALL parentheticals before similarity scoring, which makes
# "Song (Instrumental)" and "Song" score 1.0 against each other. That is the
# single biggest cause of wrong-audio matches: instrumentals, karaoke covers
# and live versions sail through with a perfect title score. These markers are
# extracted from the title's suffix segments — parentheticals, brackets, and
# text after a " - " separator — BEFORE normalization, and compared as sets.
# A mismatch means the two titles refer to different recordings of the song.
#
# Deliberately NOT markers (same recording, different packaging):
#   remaster/remastered, deluxe, anniversary, bonus, expanded, mono/stereo,
#   single version, album version, radio edit (handled by _CLEAN_VERSION_RE
#   in the resolver), feat./with credits (handled by strip_collab).
_VERSION_MARKER_CLASSES: dict[str, "re.Pattern[str]"] = {
    "instrumental": re.compile(r"\b(instrumental|inst|off vocal|karaoke|backing track)\b", re.IGNORECASE),
    "tribute":      re.compile(r"\b(tribute|cover|originally performed|in the style of)\b", re.IGNORECASE),
    "acapella":     re.compile(r"\b(acapella|a cappella|vocals? only)\b", re.IGNORECASE),
    "live":         re.compile(r"\b(live|unplugged|concert)\b", re.IGNORECASE),
    "acoustic":     re.compile(r"\bacoustic\b", re.IGNORECASE),
    "remix":        re.compile(r"\b(remix|flip|bootleg|rework|re-?edit)\b", re.IGNORECASE),
    "altered":      re.compile(r"\b(sped ?up|slowed|reverb|nightcore|8[- ]?bit|chopped|screwed|music box|lullaby)\b", re.IGNORECASE),
    "demo":         re.compile(r"\b(demo|rough mix|early version)\b", re.IGNORECASE),
    "rerecording":  re.compile(r"\b(taylor'?s version|re-?recorded|re-?recording)\b", re.IGNORECASE),
    "orchestral":   re.compile(r"\b(orchestral|orchestra version|piano version|strings version)\b", re.IGNORECASE),
}

# Suffix segments: (...) or [...] anywhere, plus everything after " - ".
_TITLE_SUFFIX_SEGMENT_RE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]|\s[-–—]\s(.+)$")


def version_markers(title: str) -> frozenset[str]:
    """Extract the set of recording-class markers from a title's suffix
    segments. 'Stand Get Up (inst)' → {'instrumental'};
    'The Boxer (Live at Central Park)' → {'live'}; 'Stand Get Up' → {}."""
    if not title:
        return frozenset()
    segments: list[str] = []
    for match in _TITLE_SUFFIX_SEGMENT_RE.finditer(title):
        segments.extend(g for g in match.groups() if g)
    if not segments:
        return frozenset()
    blob = " ".join(segments)
    found = set()
    for cls, pattern in _VERSION_MARKER_CLASSES.items():
        if pattern.search(blob):
            found.add(cls)
    return frozenset(found)


def version_mismatch(query_title: str, result_title: str) -> bool:
    """True when the two titles carry different recording-class markers —
    e.g. one is an instrumental/karaoke/live/remix and the other is not.
    Symmetric: a query for '(inst)' must not match the vocal version either."""
    return version_markers(query_title) != version_markers(result_title)


# Similarity scores are capped at this value when the recording class differs.
# Below every acceptance threshold in the resolver and all adapters.
VERSION_MISMATCH_CAP = 0.40


# Artist names that identify karaoke/tribute/cover acts. A result from one of
# these with a clean title ("God's Plan" by "Piano Tribute Players") carries no
# title marker at all — the artist name is the only signal it's the wrong
# recording. Only applied when the QUERY artists don't match the pattern too.
_TRIBUTE_ARTIST_RE = re.compile(
    r"\b(karaoke|tribute|cover(s| band| version)?|in the style of|made famous|"
    r"vitamin string quartet|rockabye baby|kidz bop|party tyme|ameritz|"
    r"8[- ]?bit arcade|lullaby|music box|sleep baby|piano dreamers)\b",
    re.IGNORECASE,
)


def _is_tribute_artist(result_artist: str, query_artists: list[str]) -> bool:
    if not result_artist or not _TRIBUTE_ARTIST_RE.search(result_artist):
        return False
    # If the requested artist is itself a karaoke/tribute act, it's a match, not a trap.
    return not any(_TRIBUTE_ARTIST_RE.search(a or "") for a in query_artists)


def score_similarity(
    query_title: str,
    query_artists: list[str],
    result_title: str,
    result_artist: str,
) -> float:
    # Recording-class gate: normalize() strips parentheticals, so without this
    # an instrumental/karaoke/live/remix version scores identically to the
    # original. Capped below every acceptance threshold, checked on the RAW
    # titles before any normalization. A karaoke/tribute act name on the result
    # artist counts as a version mismatch even when the title itself is clean.
    mismatched_version = version_mismatch(query_title, result_title) or _is_tribute_artist(
        result_artist, query_artists
    )

    def _capped(score: float) -> float:
        return min(score, VERSION_MISMATCH_CAP) if mismatched_version else score

    title_score = string_similarity(query_title, result_title)

    # Artist vs channel name
    artist_score = max(
        (string_similarity(a, result_artist) for a in query_artists),
        default=0.0,
    )

    # Artist name appearing anywhere in the video title (T-Series, Sony etc.)
    title_artist_score = max(
        (string_similarity(a, result_title) for a in query_artists),
        default=0.0,
    )

    best_artist_score = max(artist_score, title_artist_score * 0.8)

    composite = 0.60 * title_score + 0.40 * best_artist_score

    # Fallback: if title alone is a very strong match, don't let
    # a label channel name kill the result
    if title_score >= 0.55 and composite < 0.35:
        return _capped(title_score * 0.75)

    # Hard cap: if the artist is clearly wrong (score < 0.45), cap the composite
    # below LOSSLESS_ACCEPT_THRESHOLD (0.55) so a perfect title match on a
    # common song title (e.g. "White Christmas") doesn't pull in the wrong artist.
    # Only bypass this when the result has no artist info at all (empty string).
    # Exception: if the title match is very strong (≥ 0.85), the track is
    # distinctive enough to trust the title alone — skip the hard cap.
    # (The title-trust bypass does NOT apply across a version mismatch: a
    # karaoke cover with the exact same title is precisely the case it used
    # to let through.)
    if best_artist_score < 0.45 and result_artist.strip():
        if title_score >= 0.90 and not mismatched_version:
            pass  # distinctive title — trust it
        elif title_score >= 0.75:
            return _capped(min(composite, 0.55))  # moderate confidence — softer cap
        else:
            return _capped(min(composite, 0.50))

    return _capped(composite)


def duration_close(expected_s: float, actual_s: float, tolerance: int = 10) -> bool:
    """Return True if durations are within an adaptive tolerance window.

    Cross-service catalogs often disagree by a few extra seconds because of
    leading silence, trailing fade-outs, regional edits, or metadata rounding.
    A hard 5-second cutoff is too strict for long tracks and DJ mixes, so we
    keep the caller-provided tolerance as a floor and expand it slightly for
    longer recordings.
    """
    try:
        expected = float(expected_s)
        actual = float(actual_s)
    except (TypeError, ValueError):
        return False

    longer = max(expected, actual)
    adaptive_tolerance = min(30.0, longer * 0.045)
    effective_tolerance = max(float(tolerance), adaptive_tolerance)
    return abs(expected - actual) <= effective_tolerance
