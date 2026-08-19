"""
Persistent lyrics cache (v1.1.8 FEAT-9).

Lyrics resolution is expensive: the waterfall can hit several providers per
track, and Apple lyrics are served by a *very* small pool of entitled accounts
(measured 2026-07-30: 2 of 17 credential files). Re-downloading an album must
not re-query every provider for every track, or that thin pool becomes the
bottleneck for the whole library.

Follows the FEAT-6 ISRC cache exactly — SQLite in the platformdirs user-data
dir, every operation degrading to a no-op on failure — with two differences that
matter for lyrics specifically:

  * **Synced and plain are stored together**, and an entry that holds only plain
    text is re-queried sooner. Otherwise the first plain-only hit would be cached
    for a month and the user would never get synced lyrics for that track, which
    is precisely the failure this feature exists to fix.
  * **The provider is recorded**, so if one source is later found to be serving
    bad matches its entries can be dropped without discarding the cache.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# A synced hit is worth keeping for a long time — it will not improve.
SYNCED_TTL_SECONDS = 90 * 24 * 3600
# A plain-only hit is re-checked much sooner: providers add timings over time,
# and settling permanently for plain is the exact complaint FEAT-9 addresses.
PLAIN_TTL_SECONDS = 7 * 24 * 3600
# "Nothing anywhere" — cheap to recheck, and catalogues do gain lyrics.
NEGATIVE_TTL_SECONDS = 3 * 24 * 3600

_INSTANCES: dict[str, "LyricsCache"] = {}
_INSTANCES_LOCK = threading.Lock()


def get_lyrics_cache(db_path: Optional[str] = None) -> Optional["LyricsCache"]:
    path = db_path or LyricsCache.default_path()
    if not path:
        return None
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            try:
                inst = LyricsCache(Path(path))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[LyricsCache] disabled (could not open {path}): {e}")
                return None
            _INSTANCES[path] = inst
        return inst


class LyricsCache:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS lyrics (
        key        TEXT PRIMARY KEY,
        plain      TEXT,
        synced     TEXT,
        provider   TEXT,
        updated_at INTEGER DEFAULT 0
    )"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(self._SCHEMA)
            self._db.commit()

    @staticmethod
    def default_path() -> str:
        from antra.utils.runtime import get_app_data_dir
        return str(get_app_data_dir() / "lyrics_cache.db")

    @staticmethod
    def make_key(track) -> str:
        """ISRC when available — it is the only stable identifier. Otherwise fall
        back to a normalised title/artist/duration triple, with duration bucketed
        so a one-second metadata difference does not miss the cache."""
        isrc = (getattr(track, "isrc", None) or "").strip().upper()
        if isrc:
            return f"isrc:{isrc}"
        title = (getattr(track, "title", "") or "").strip().lower()
        artist = (getattr(track, "primary_artist", "") or "").strip().lower()
        dur = getattr(track, "duration_ms", None) or 0
        return f"ta:{title}|{artist}|{int(dur / 5000)}"

    def get(self, track) -> Optional[tuple[str, str, str]]:
        """Return (plain, synced, provider) or None when absent/expired."""
        key = self.make_key(track)
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT plain, synced, provider, updated_at FROM lyrics WHERE key = ?",
                    (key,),
                ).fetchone()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LyricsCache] get failed: {e}")
            return None
        if not row:
            return None
        plain, synced, provider, updated = (row[0] or ""), (row[1] or ""), (row[2] or ""), int(row[3] or 0)
        if synced:
            ttl = SYNCED_TTL_SECONDS
        elif plain:
            ttl = PLAIN_TTL_SECONDS
        else:
            ttl = NEGATIVE_TTL_SECONDS
        if time.time() - updated > ttl:
            return None
        return plain, synced, provider

    def put(self, track, plain: str, synced: str, provider: str = "") -> None:
        key = self.make_key(track)
        try:
            with self._lock:
                self._db.execute(
                    """
                    INSERT INTO lyrics (key, plain, synced, provider, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        plain = excluded.plain,
                        synced = excluded.synced,
                        provider = excluded.provider,
                        updated_at = excluded.updated_at
                    """,
                    (key, plain or "", synced or "", provider or "", int(time.time())),
                )
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LyricsCache] put failed: {e}")

    def stats(self) -> dict:
        try:
            with self._lock:
                total = self._db.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0]
                synced = self._db.execute(
                    "SELECT COUNT(*) FROM lyrics WHERE synced != ''"
                ).fetchone()[0]
                plain = self._db.execute(
                    "SELECT COUNT(*) FROM lyrics WHERE synced = '' AND plain != ''"
                ).fetchone()[0]
        except Exception:  # pragma: no cover - defensive
            return {"total": 0, "synced": 0, "plain": 0, "negative": 0}
        return {"total": total, "synced": synced, "plain": plain,
                "negative": total - synced - plain}
