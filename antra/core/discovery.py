import json
import logging
import os
import time
from typing import Optional

import requests

from antra.core.apple_fetcher import AppleFetcher
from antra.utils.config import get_config_dir

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

class AppleDiscovery:
    """
    Fetches and caches Top Charts and Genre Playlists from Apple Music.
    """
    def __init__(self):
        self.fetcher = AppleFetcher()
        self.cache_dir = os.path.join(get_config_dir(), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "apple_discovery.json")

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"[Discovery] Failed to load cache: {e}")
        return {}

    def _save_cache(self, data: dict):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[Discovery] Failed to save cache: {e}")

    def get_discovery_data(self, storefront: str = "us", genre_id: Optional[str] = None, genre_name: Optional[str] = None) -> dict:
        cache = self._load_cache()
        key = f"{storefront}_{genre_id or 'all'}"
        
        now = time.time()
        cached_entry = cache.get(key)
        if cached_entry and now - cached_entry.get("timestamp", 0) < CACHE_TTL_SECONDS:
            cached_data = cached_entry["data"]
            # Don't serve a cached empty result — it was likely from a previous failed fetch.
            # Force a fresh network call so the user sees real charts.
            has_content = (
                bool(cached_data.get("top_albums"))
                or bool(cached_data.get("top_playlists"))
                or bool(cached_data.get("genre_albums"))
                or bool(cached_data.get("genre_playlists"))
            )
            if has_content:
                return cached_data

        logger.info(f"[Discovery] Fetching fresh discovery data for {storefront} (genre: {genre_name})")
        data = self._fetch_fresh_data(storefront, genre_id, genre_name)
        
        cache[key] = {
            "timestamp": now,
            "data": data
        }
        self._save_cache(cache)
        return data

    def get_genres(self, storefront: str = "us") -> list:
        cache = self._load_cache()
        key = f"genres_{storefront}"
        now = time.time()
        
        cached_entry = cache.get(key)
        if cached_entry and now - cached_entry.get("timestamp", 0) < CACHE_TTL_SECONDS:
            # Don't serve an empty genres cache — force a fresh fetch
            if cached_entry["data"]:
                return cached_entry["data"]

        token = self.fetcher._get_developer_token()
        if not token:
            return []

        try:
            res = requests.get(
                f"https://api.music.apple.com/v1/catalog/{storefront}/genres",
                headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
                timeout=15
            )
            if res.ok:
                genres = []
                for g in res.json().get("data", []):
                    # Filter out parent containers like 'Music'
                    if g["attributes"]["name"] != "Music":
                        genres.append({
                            "id": g["id"],
                            "name": g["attributes"]["name"]
                        })
                # Sort alphabetically
                genres.sort(key=lambda x: x["name"])
                
                cache[key] = {
                    "timestamp": now,
                    "data": genres
                }
                self._save_cache(cache)
                return genres
        except Exception as e:
            logger.debug(f"[Discovery] Failed to fetch genres: {e}")

        return []

    def _fetch_fresh_data(self, storefront: str, genre_id: Optional[str], genre_name: Optional[str]) -> dict:
        token = self.fetcher._get_developer_token()
        if not token:
            return {"top_albums": [], "top_playlists": [], "genre_playlists": []}

        headers = {"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}
        data = {
            "top_albums": [],
            "top_playlists": [],
            "genre_playlists": []
        }

        # 1. Fetch Top Albums (Filtered by genre if provided)
        charts_url = f"https://api.music.apple.com/v1/catalog/{storefront}/charts?types=albums&limit=20"
        if genre_id:
            charts_url += f"&genre={genre_id}"
            
        try:
            res = requests.get(charts_url, headers=headers, timeout=15)
            if res.ok:
                results = res.json().get("results", {})
                if "albums" in results and results["albums"]:
                    for item in results["albums"][0].get("data", []):
                        data["top_albums"].append(self._format_item(item, storefront, "album"))
        except Exception as e:
            logger.debug(f"[Discovery] Failed to fetch top albums: {e}")

        # 2. Fetch Top Playlists (Global, only if no genre is selected)
        if not genre_id:
            charts_url = f"https://api.music.apple.com/v1/catalog/{storefront}/charts?types=playlists&limit=20"
            try:
                res = requests.get(charts_url, headers=headers, timeout=15)
                if res.ok:
                    results = res.json().get("results", {})
                    if "playlists" in results and results["playlists"]:
                        for item in results["playlists"][0].get("data", []):
                            data["top_playlists"].append(self._format_item(item, storefront, "playlist"))
            except Exception as e:
                logger.debug(f"[Discovery] Failed to fetch top playlists: {e}")
        else:
            # 3. Fetch Genre Playlists via Search
            search_url = f"https://api.music.apple.com/v1/catalog/{storefront}/search?types=playlists&limit=20&term={genre_name}"
            try:
                res = requests.get(search_url, headers=headers, timeout=15)
                if res.ok:
                    results = res.json().get("results", {})
                    if "playlists" in results and results["playlists"]:
                        for item in results["playlists"].get("data", []):
                            data["genre_playlists"].append(self._format_item(item, storefront, "playlist"))
            except Exception as e:
                logger.debug(f"[Discovery] Failed to fetch genre playlists: {e}")

        return data

    def _format_item(self, item: dict, storefront: str, item_type: str) -> dict:
        attrs = item.get("attributes", {})
        
        artwork_url = ""
        art = attrs.get("artwork", {})
        if art:
            w = art.get("width", 600)
            h = art.get("height", 600)
            artwork_url = art.get("url", "").replace("{w}", str(w)).replace("{h}", str(h))

        url = attrs.get("url", "")
        # Ensure it has a valid apple music URL if API didn't provide full one
        if not url:
            if item_type == "album":
                url = f"https://music.apple.com/{storefront}/album/{item['id']}"
            elif item_type == "playlist":
                url = f"https://music.apple.com/{storefront}/playlist/{item['id']}"

        return {
            "id": item.get("id"),
            "type": item_type,
            "name": attrs.get("name", "Unknown"),
            "artist_name": attrs.get("artistName", ""),
            "curator_name": attrs.get("curatorName", ""),
            "artwork_url": artwork_url,
            "url": url
        }


# ── Spotify ───────────────────────────────────────────────────────────────────

# Persisted-query hash for the web player's `home` operation. Extracted from
# open.spotifycdn.com's bundle; Spotify rotates these, so a rotation shows up as
# an empty Discover tab rather than a crash (see _fetch_fresh_data).
_SPOTIFY_HOME_HASH = "76243c78b0e20ecdbe41b794dec8cbe73f75e585b0a7201b8d2e84578412847a"

# The home feed is PERSONALISED, so its cache must be keyed per account and must
# expire far sooner than Apple's static charts.
SPOTIFY_CACHE_TTL_SECONDS = 6 * 60 * 60


class SpotifyDiscovery:
    """Discovery data from Spotify's personalised home feed.

    Deliberately returns the exact same contract as AppleDiscovery
    (`top_albums` / `top_playlists` / `genre_playlists`, and the same item
    shape) so the Go layer and the UI need no per-source special-casing.

    Requires `sp_dc` — the feed is per-user and there is no logged-out
    equivalent, so the caller is responsible for falling back to Apple when no
    account is connected.
    """

    def __init__(self, sp_dc: str):
        self.sp_dc = (sp_dc or "").strip()
        self.cache_dir = os.path.join(get_config_dir(), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "spotify_discovery.json")

    # ── cache (same shape as AppleDiscovery, separate file) ──────────────────

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"[Discovery] Failed to load Spotify cache: {e}")
        return {}

    def _save_cache(self, data: dict):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[Discovery] Failed to save Spotify cache: {e}")

    def _cache_key(self) -> str:
        # Never share a personalised feed between accounts.
        import hashlib
        return "home_" + hashlib.sha256(self.sp_dc.encode("utf-8")).hexdigest()[:16]

    def get_genres(self, storefront: str = "us") -> list:
        """Spotify's home feed has no genre facet equivalent to Apple's charts.

        Returns an empty list rather than inventing categories that cannot
        actually filter the feed — the UI hides the genre picker when empty.
        """
        return []

    def get_discovery_data(self, storefront: str = "us", genre_id: Optional[str] = None,
                           genre_name: Optional[str] = None) -> dict:
        if not self.sp_dc:
            return {"top_albums": [], "top_playlists": [], "genre_playlists": []}

        cache = self._load_cache()
        key = self._cache_key()
        now = time.time()
        entry = cache.get(key)
        if entry and now - entry.get("timestamp", 0) < SPOTIFY_CACHE_TTL_SECONDS:
            cached = entry.get("data") or {}
            # Same rule as Apple: never serve a cached empty result, it was
            # almost certainly a failed fetch.
            if cached.get("top_albums") or cached.get("top_playlists"):
                return cached

        logger.info("[Discovery] Fetching fresh Spotify home feed")
        data = self._fetch_fresh_data()
        cache[key] = {"timestamp": now, "data": data}
        self._save_cache(cache)
        return data

    # ── fetch ────────────────────────────────────────────────────────────────

    def _fetch_fresh_data(self) -> dict:
        empty = {"top_albums": [], "top_playlists": [], "genre_playlists": []}
        try:
            from antra.core.spotify_library import SpotifyLibraryClient
        except Exception as e:
            logger.debug(f"[Discovery] Spotify client unavailable: {e}")
            return empty

        try:
            client = SpotifyLibraryClient(self.sp_dc)
            variables = {
                "homeEndUserIntegration": "INTEGRATION_WEB_PLAYER",
                "timeZone": "UTC",
                "sp_t": "",
                "facet": None,
                "sectionItemsLimit": 10,
                "includeEpisodeContentRatingsV2": True,
            }
            payload = client._graphql("home", variables, _SPOTIFY_HOME_HASH)
        except Exception as e:
            # A rotated persisted-query hash lands here. Fail soft — the caller
            # falls back to Apple rather than showing the user an error.
            logger.warning(f"[Discovery] Spotify home feed failed: {e}")
            return empty

        albums, playlists = [], []
        seen: set[str] = set()
        try:
            sections = (
                ((payload or {}).get("data") or {})
                .get("home", {})
                .get("sectionContainer", {})
                .get("sections", {})
                .get("items", [])
            ) or []
        except Exception:
            sections = []

        for section in sections:
            for item in ((section.get("sectionItems") or {}).get("items") or []):
                node = ((item.get("content") or {}).get("data") or {})
                kind = node.get("__typename")
                if kind not in ("Album", "Playlist"):
                    continue
                formatted = self._format_node(node, kind)
                if not formatted or formatted["id"] in seen:
                    continue
                seen.add(formatted["id"])
                (albums if kind == "Album" else playlists).append(formatted)

        return {
            "top_albums": albums[:40],
            "top_playlists": playlists[:40],
            "genre_playlists": [],
        }

    # ── formatting ───────────────────────────────────────────────────────────

    @staticmethod
    def _best_image(node: dict) -> str:
        """Largest available cover. The home feed uses several shapes depending
        on item type, so try each rather than assuming one."""
        candidates = []
        cover = node.get("coverArt") or {}
        candidates.extend(cover.get("sources") or [])
        images = (node.get("images") or {}).get("items") or []
        for entry in images:
            candidates.extend(entry.get("sources") or [])
        if not candidates:
            return ""
        best = max(candidates, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        return best.get("url", "") or ""

    @classmethod
    def _format_node(cls, node: dict, kind: str) -> Optional[dict]:
        uri = node.get("uri") or ""
        # spotify:album:ID -> ID
        spotify_id = uri.rsplit(":", 1)[-1] if ":" in uri else ""
        if not spotify_id:
            return None

        if kind == "Album":
            artists = ((node.get("artists") or {}).get("items") or [])
            artist_name = ", ".join(
                (a.get("profile") or {}).get("name", "") for a in artists
            ).strip(", ")
            curator = ""
            item_type = "album"
        else:
            artist_name = ""
            owner = node.get("ownerV2") or node.get("owner") or {}
            curator = ((owner.get("data") or owner) or {}).get("name", "") or ""
            item_type = "playlist"

        return {
            "id": spotify_id,
            "type": item_type,
            "name": node.get("name") or "Unknown",
            "artist_name": artist_name,
            "curator_name": curator,
            "artwork_url": cls._best_image(node),
            "url": f"https://open.spotify.com/{item_type}/{spotify_id}",
        }
