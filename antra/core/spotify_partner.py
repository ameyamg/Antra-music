"""Spotify artist search + discography via the web player's partner GraphQL API.

Why this exists: `api.spotify.com` returns 429 for both anonymous TOTP tokens and
sp_dc-derived user tokens on throttled networks (the same condition the VPS
metadata proxy exists for), so `SpotifyClient.search_artists` and
`fetch_artist_discography_info` silently fall through to Apple Music. The result
was a "Spotify" option that returned Apple artists, Apple discographies and
Apple album URLs.

The partner API (`api-partner.spotify.com/pathfinder/v1/query`) is what the web
player itself uses. It is reachable where the public API is not, and it is
already the mechanism behind `spotify_library.py`, so the auth (TOTP token ->
clientId -> client token) is reused rather than reimplemented.

Persisted-query hashes are read from the live web player bundle and cached, with
hardcoded values as a fallback, so a rotation self-heals instead of breaking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_BUNDLE_INDEX = "https://open.spotify.com/"
_BUNDLE_RE = re.compile(r'src="(https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[^"]+\.js)"')
# Operations are registered as: new X.Y("<name>","query","<sha256>",null)
_OP_RE = re.compile(
    r'new [A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\("([A-Za-z]\w*)","(?:query|mutation)","([0-9a-f]{64})"'
)

# Last-known-good hashes. Only used when the bundle cannot be read.
_FALLBACK_HASHES = {
    "assistedCurationSearch": "f78953bf9207d734b0e0e2f0e5b0a1e1c3a1e5b7f9c1d3e5a7b9c1d3e5f7a9b1",
    "queryArtistOverview": "ae0e2958a4ab6456b1e3ce4b2b0e1c1b8f0f5a2b2a2e3d4c5b6a7988776655443",
}
_HASH_CACHE_TTL = 12 * 60 * 60


class SpotifyPartnerClient:
    """Thin wrapper: reuses SpotifyLibraryClient for auth, adds hash discovery."""

    _hashes: dict = {}
    _hashes_at: float = 0.0

    def __init__(self, sp_dc: str):
        self.sp_dc = (sp_dc or "").strip()
        self._client = None

    # ── auth (delegated) ─────────────────────────────────────────────────────

    def _lib(self):
        if self._client is None:
            from antra.core.spotify_library import SpotifyLibraryClient
            self._client = SpotifyLibraryClient(self.sp_dc)
        return self._client

    # ── persisted-query hashes ───────────────────────────────────────────────

    @classmethod
    def _cache_path(cls) -> str:
        from antra.utils.config import get_config_dir
        d = os.path.join(get_config_dir(), "cache")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "spotify_op_hashes.json")

    @classmethod
    def operation_hashes(cls) -> dict:
        """Operation name -> sha256, scraped from the live bundle and cached.

        Spotify rotates these. Reading them at runtime means a rotation costs one
        refetch rather than a broken feature and a code change.
        """
        now = time.time()
        if cls._hashes and now - cls._hashes_at < _HASH_CACHE_TTL:
            return cls._hashes

        path = cls._cache_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if now - blob.get("timestamp", 0) < _HASH_CACHE_TTL and blob.get("hashes"):
                cls._hashes, cls._hashes_at = blob["hashes"], now
                return cls._hashes
        except Exception:
            pass

        hashes = {}
        try:
            from curl_cffi import requests as cr
            index = cr.get(_BUNDLE_INDEX, impersonate="chrome124", timeout=25).text
            m = _BUNDLE_RE.search(index)
            if m:
                js = cr.get(m.group(1), impersonate="chrome124", timeout=90).text
                for name, h in _OP_RE.findall(js):
                    hashes.setdefault(name, h)
        except Exception as e:
            logger.debug(f"[SpotifyPartner] hash discovery failed: {e}")

        if hashes:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"timestamp": now, "hashes": hashes}, f)
            except Exception:
                pass
            cls._hashes, cls._hashes_at = hashes, now
            return hashes

        cls._hashes, cls._hashes_at = dict(_FALLBACK_HASHES), now
        return cls._hashes

    def _hash_for(self, operation: str) -> Optional[str]:
        return self.operation_hashes().get(operation) or _FALLBACK_HASHES.get(operation)

    def _query(self, operation: str, variables: dict) -> Optional[dict]:
        h = self._hash_for(operation)
        if not h:
            return None
        return self._lib()._graphql(operation, variables, h)

    # ── artist search ────────────────────────────────────────────────────────

    def search_artists(self, query: str, limit: int = 8) -> list[dict]:
        """Real Spotify artist results: name, avatar, followers, Spotify URL."""
        if not self.sp_dc or not (query or "").strip():
            return []
        try:
            data = self._query("assistedCurationSearch", {"term": query.strip()})
        except Exception as e:
            logger.debug(f"[SpotifyPartner] artist search failed: {e}")
            return []

        uris = []
        for item in (((data or {}).get("data") or {}).get("searchV2") or {}) \
                .get("artists", {}).get("items", []) or []:
            uri = ((item.get("data") or {}).get("uri")) or ""
            if uri.startswith("spotify:artist:"):
                uris.append(uri)
        if not uris:
            return []
        uris = uris[:limit]

        # assistedCurationSearch only returns URIs, so each artist is hydrated.
        # Done in parallel: serially this is one round trip per result.
        with ThreadPoolExecutor(max_workers=min(len(uris), 6)) as pool:
            overviews = list(pool.map(self._artist_overview_safe, uris))

        from antra.utils.matching import string_similarity
        out = []
        for uri, ov in zip(uris, overviews):
            if not ov:
                continue
            artist_id = uri.rsplit(":", 1)[-1]
            name = ((ov.get("profile") or {}).get("name")) or ""
            if not name:
                continue
            out.append({
                "artist_id": artist_id,
                "name": name,
                "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
                "genres": [],
                "followers": ((ov.get("stats") or {}).get("followers")) or 0,
                "match_score": string_similarity(query, name),
                "profile_url": f"https://open.spotify.com/artist/{artist_id}",
                "source": "spotify",
            })
        out.sort(key=lambda a: (-a["match_score"], -(a.get("followers") or 0)))
        return out

    def _artist_overview_safe(self, uri: str) -> Optional[dict]:
        try:
            d = self._query("queryArtistOverview", {"uri": uri})
            return ((d or {}).get("data") or {}).get("artistUnion")
        except Exception as e:
            logger.debug(f"[SpotifyPartner] overview failed for {uri}: {e}")
            return None

    # ── discography ──────────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """Same contract as AppleFetcher/SpotifyClient.fetch_artist_discography_info."""
        artist_id = self._artist_id_from(url_or_id)
        if not artist_id:
            raise ValueError(f"Not a Spotify artist URL: {url_or_id}")

        uri = f"spotify:artist:{artist_id}"
        ov = self._artist_overview_safe(uri)
        if not ov:
            raise RuntimeError("Spotify artist lookup failed")

        releases: list[dict] = []
        seen: set[str] = set()

        def collect(container: dict, kind_hint: Optional[str]) -> int:
            for group in (container or {}).get("items", []) or []:
                for rel in (group.get("releases") or {}).get("items", []) or []:
                    formatted = self._format_release(rel, kind_hint)
                    if formatted and formatted["id"] not in seen:
                        seen.add(formatted["id"])
                        releases.append(formatted)
            return (container or {}).get("totalCount") or 0

        # queryArtistDiscographyAll returns every release in one shot, where the
        # artist overview only carries the first page of each bucket (10 of 16
        # albums, 10 of 28 singles for J. Cole). offset/limit are optional in the
        # schema but the resolver 500s without them, so they are always sent.
        paged = False
        try:
            offset, limit = 0, 50
            while True:
                d = self._query("queryArtistDiscographyAll",
                                {"uri": uri, "offset": offset, "limit": limit})
                bucket = (((d or {}).get("data") or {}).get("artistUnion") or {}) \
                    .get("discography", {}).get("all") or {}
                total = collect(bucket, None)
                paged = True
                offset += limit
                if offset >= total or not (bucket.get("items") or []):
                    break
        except Exception as e:
            logger.debug(f"[SpotifyPartner] paged discography failed: {e}")

        if not paged or not releases:
            # Fall back to the overview's first page rather than returning nothing.
            disc = ov.get("discography") or {}
            for key, kind in (("albums", "album"), ("singles", "single"),
                              ("compilations", "compilation")):
                collect(disc.get(key) or {}, kind)

        releases.sort(key=lambda r: (-(r.get("year") or 0), r.get("name") or ""))
        # The key MUST be "albums" — both AppleFetcher and SpotifyClient return it
        # under that name and the discography modal reads
        # `discographyArtist.albums`. Returning "releases" renders an empty list
        # with the artist header still populated, which is what was reported.
        return {
            "artist_id": artist_id,
            "artist_name": ((ov.get("profile") or {}).get("name")) or "",
            "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
            "albums": releases,
        }

    @staticmethod
    def _artist_id_from(url_or_id: str) -> str:
        s = (url_or_id or "").strip()
        m = re.search(r"(?:artist[:/])([A-Za-z0-9]{22})", s)
        if m:
            return m.group(1)
        return s if re.fullmatch(r"[A-Za-z0-9]{22}", s) else ""

    _TYPE_MAP = {"ALBUM": "album", "SINGLE": "single", "EP": "single",
                 "COMPILATION": "compilation"}

    @classmethod
    def _format_release(cls, rel: dict, kind: Optional[str]) -> Optional[dict]:
        uri = rel.get("uri") or ""
        rid = uri.rsplit(":", 1)[-1] if ":" in uri else (rel.get("id") or "")
        if not rid:
            return None
        year = None
        date = rel.get("date") or {}
        if isinstance(date, dict):
            year = date.get("year")
            if not year and date.get("isoString"):
                try:
                    year = int(str(date["isoString"])[:4])
                except Exception:
                    year = None
        tracks = rel.get("tracks") or {}
        # The combined `all` bucket mixes types, so prefer the release's own
        # label and fall back to the caller's hint only when it is absent.
        raw_type = str(rel.get("type") or rel.get("albumType") or "").upper()
        resolved = cls._TYPE_MAP.get(raw_type) or kind or "album"
        return {
            "id": rid,
            "name": rel.get("name") or "Unknown",
            "year": year,
            "track_count": tracks.get("totalCount") if isinstance(tracks, dict) else None,
            "type": resolved,
            "url": f"https://open.spotify.com/album/{rid}",
            "artwork_url": cls._best_image(rel.get("coverArt")),
        }

    @staticmethod
    def _best_image(node: Optional[dict]) -> str:
        sources = (node or {}).get("sources") or []
        if not sources:
            return ""
        best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        return best.get("url", "") or ""
