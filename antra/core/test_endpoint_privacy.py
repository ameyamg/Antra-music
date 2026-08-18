#!/usr/bin/env python3
"""Prove the backend cannot reach the public gist once the desktop layer has
supplied endpoints (v1.1.8 FEAT-8 Phase A).

The guard is only worth anything if it makes the call IMPOSSIBLE rather than
unlikely, so the network is sabotaged for the duration: any HTTP attempt raises
and fails the test loudly instead of silently succeeding against the real gist.

Run: python antra/core/test_endpoint_privacy.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests  # noqa: E402

from antra.core.endpoint_manifest import (  # noqa: E402
    load_endpoint_manifest,
    manifest_fetch_disabled,
)

failures = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


class NetworkUsed(AssertionError):
    pass


class Tripwire:
    """Any outbound HTTP during the guarded window is a failure."""

    def __enter__(self):
        self._get, self._req = requests.get, requests.Session.request

        def boom(*a, **k):
            raise NetworkUsed(f"network call attempted: {a[:2]}")

        requests.get = boom
        requests.Session.request = boom
        return self

    def __exit__(self, *exc):
        requests.get, requests.Session.request = self._get, self._req
        return False


print("\n1. The flag itself")
for val, want in (("1", True), ("true", True), ("YES", True), ("0", False), ("", False)):
    os.environ["ANTRA_ENDPOINT_MANIFEST_DISABLED"] = val
    check(f"{val!r} -> {want}", manifest_fetch_disabled() is want)
os.environ.pop("ANTRA_ENDPOINT_MANIFEST_DISABLED", None)
check("unset -> False (unchanged behaviour for everyone not signed in)", manifest_fetch_disabled() is False)

print("\n2. With the flag set, NO network call is made")
os.environ["ANTRA_ENDPOINT_MANIFEST_DISABLED"] = "1"
try:
    with Tripwire():
        m = load_endpoint_manifest()
    check("load_endpoint_manifest made no request", True)
    check("returned a manifest object", m is not None)
except NetworkUsed as exc:
    check(f"load_endpoint_manifest made no request ({exc})", False)

print("\n3. The Apple gist fallback is skipped too (it was the last gist reader)")
try:
    from antra.core.service import _fetch_gist_apple_mirror

    with Tripwire():
        got = _fetch_gist_apple_mirror(None)
    check("_fetch_gist_apple_mirror returned '' without calling out", got == "")
except NetworkUsed as exc:
    check(f"_fetch_gist_apple_mirror hit the network ({exc})", False)
except Exception as exc:  # import problems must not read as a pass
    check(f"_fetch_gist_apple_mirror importable ({exc})", False)

print("\n4. Without the flag the gist path is still reachable (no silent regression)")
os.environ.pop("ANTRA_ENDPOINT_MANIFEST_DISABLED", None)
tried = {"n": 0}
real_get = requests.get


def counting_get(*a, **k):
    tried["n"] += 1
    raise requests.exceptions.ConnectionError("blocked by test")


requests.get = counting_get
try:
    from antra.core.service import _fetch_gist_apple_mirror as f2

    f2(None)
finally:
    requests.get = real_get
check("it attempted the fetch when not disabled", tried["n"] >= 1)

os.environ.pop("ANTRA_ENDPOINT_MANIFEST_DISABLED", None)
print(f"\n{'ALL PASS' if not failures else 'FAILURES: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
