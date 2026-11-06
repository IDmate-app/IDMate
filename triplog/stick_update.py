"""Is there newer IDTelemetry firmware than the stick runs?

The server asks GitHub (not the stick: no data over the SIM). Releases of
the firmware repo are tagged vX.Y.Z (pre-releases included — the firmware is
beta). Result cached for hours; a failed lookup is retried after a while and
never blocks a request: the first call starts the lookup in the background.

Off with STICK_UPDATE_CHECK=0 (then the server makes no request to GitHub).
"""

import json
import re
import threading
import time
import urllib.request

import config

CACHE_S = 6 * 3600
RETRY_S = 30 * 60
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_lock = threading.Lock()
_cache = {"t": 0.0, "data": None, "busy": False}


def version_tuple(v):
    m = _TAG.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def parse_releases(items):
    """GitHub release list → newest {version, url, assets} or None."""
    best = None
    for r in items or []:
        if r.get("draft"):
            continue
        vt = version_tuple(r.get("tag_name"))
        if not vt or (best and vt <= best[0]):
            continue
        assets = {}
        for a in r.get("assets") or []:
            m = re.match(r"^idtelemetry-(full|lite)-[\d.]+\.bin$", a.get("name", ""))
            if m:
                dig = a.get("digest") or ""
                assets[m.group(1)] = {"name": a["name"], "url": a.get("browser_download_url"),
                                      "size": a.get("size"),
                                      "sha256": dig[7:] if dig.startswith("sha256:") else None}
        best = (vt, {"version": ".".join(map(str, vt)), "url": r.get("html_url"), "assets": assets})
    return best[1] if best else None


def _fetch():
    url = f"https://api.github.com/repos/{config.STICK_FW_REPO}/releases?per_page=10"
    req = urllib.request.Request(url, headers={"User-Agent": "IDmate", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return parse_releases(json.loads(r.read().decode("utf-8")))


def _refresh():
    data = None
    try:
        data = _fetch()
    except Exception:
        data = None
    with _lock:
        _cache["busy"] = False
        if data:
            _cache.update(t=time.time(), data=data)
        else:   # keep the old result, try again after RETRY_S
            _cache["t"] = time.time() - CACHE_S + RETRY_S


def latest():
    """Newest release (cached) or None; never waits for GitHub."""
    if not config.STICK_UPDATE_CHECK:
        return None
    with _lock:
        stale = time.time() - _cache["t"] > CACHE_S
        if stale and not _cache["busy"]:
            _cache["busy"] = True
            threading.Thread(target=_refresh, daemon=True).start()
        return _cache["data"]


def update_for(stick, rel=None):
    """{version, url, size} when `rel` (default: latest()) is newer than the
    stick's firmware, else None."""
    if not stick or not stick.get("fw"):
        return None
    rel = rel if rel is not None else latest()
    cur, new = version_tuple(stick["fw"]), version_tuple((rel or {}).get("version"))
    if not cur or not new or new <= cur:
        return None
    asset = (rel.get("assets") or {}).get(stick.get("var") or "full") or {}
    return {"version": rel["version"], "url": rel.get("url"), "size": asset.get("size"),
            "file": asset.get("name"), "file_url": asset.get("url")}
