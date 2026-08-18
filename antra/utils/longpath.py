r"""
Windows extended-length path support (v1.1.8 BUG-6).

Windows caps normal paths at 260 characters (`MAX_PATH`), and `LongPathsEnabled`
is 0 on a default install. Past that limit Python cannot see a file *at all* —
`open()`, `os.path.getsize()`, `os.path.exists()` and mutagen all fail with
`FileNotFoundError` — while **ffmpeg succeeds**, because it uses the wide Win32
APIs. That asymmetry is what made BUG-6 so confusing: ffmpeg-backed code paths
(remux, segment concat, transcode) happily created files Python then reported as
missing, and the track was still marked `[Complete]`.

Measured on a default Windows 11 install with `LongPathsEnabled = 0`:

    path length 280
    plain open(w)        : FAIL errno 2
    extended-prefix open : OK
    getsize plain        : FAIL errno 2   |  getsize prefixed : 4096
    exists  plain        : False          |  exists  prefixed : True

Prefixing with `\\?\` bypasses the limit entirely. The prefix requires an
**absolute, fully-normalised** path — no forward slashes, no `.` or `..`
components — so `extended_path()` normalises before applying it.

This is a no-op on non-Windows platforms and for paths that are comfortably
short, so it can be applied unconditionally at call sites.
"""
from __future__ import annotations

import os
import sys

# Applied a little below the real 260 limit: a caller often appends an extension
# or a ".part" suffix after building the base path, and those few characters must
# not be what tips it over.
LONG_PATH_THRESHOLD = 240

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def is_windows() -> bool:
    return sys.platform == "win32"


def extended_path(path: str) -> str:
    r"""Return `path` usable by Python's file APIs regardless of its length.

    On Windows, long paths get the `\\?\` extended-length prefix. Everywhere
    else — and for already-prefixed or short paths — the input is returned
    unchanged.
    """
    if not path or not is_windows():
        return path
    if path.startswith(_PREFIX):
        return path
    if len(path) < LONG_PATH_THRESHOLD:
        return path
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return path
    # The extended-length form does not accept forward slashes or relative
    # components; abspath already normalises both on Windows.
    if abs_path.startswith("\\\\"):
        # UNC share: \\server\share\... becomes \\?\UNC\server\share\...
        return _UNC_PREFIX + abs_path.lstrip("\\")
    return _PREFIX + abs_path


def shorten_for_display(path: str) -> str:
    r"""Strip the extended-length prefix again for logs and user-facing messages.

    Users should never see `\\?\` in an error — it is an implementation detail
    and looks like corruption.
    """
    if not path:
        return path
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_PREFIX):
        return path[len(_PREFIX):]
    return path


def path_too_long(path: str) -> bool:
    """True when this path would exceed the classic Windows limit.

    Used to explain a failure in plain language rather than surfacing a bare
    `[Errno 2] No such file or directory`, which reads as "the download broke"
    when the real cause is the library folder being nested too deeply.
    """
    return bool(path) and is_windows() and len(path) >= 260
