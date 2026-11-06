"""Stick firmware update hint: newest IDTelemetry release vs. what the stick
reports (info telegram)."""

import stick_update as su

RELEASES = [
    {"tag_name": "v1.5.2", "draft": False, "html_url": "https://x/r/v1.5.2", "assets": []},
    {"tag_name": "v1.6.1", "draft": False, "prerelease": True, "html_url": "https://x/r/v1.6.1",
     "assets": [{"name": "idtelemetry-full-1.6.1.bin", "browser_download_url": "https://x/f.bin",
                 "size": 1507681, "digest": "sha256:ab12"},
                {"name": "idtelemetry-lite-1.6.1.bin", "browser_download_url": "https://x/l.bin", "size": 1458577},
                {"name": "idtelemetry-full-1.6.1.bootloader.bin", "size": 15104}]},
    {"tag_name": "v1.7.0", "draft": True, "assets": []},          # draft: ignored
    {"tag_name": "nightly", "draft": False, "assets": []},        # no version: ignored
]


def test_newest_release_incl_prerelease_without_drafts():
    r = su.parse_releases(RELEASES)
    assert r["version"] == "1.6.1" and r["url"] == "https://x/r/v1.6.1"
    assert r["assets"]["full"] == {"name": "idtelemetry-full-1.6.1.bin", "url": "https://x/f.bin",
                                   "size": 1507681, "sha256": "ab12"}
    assert r["assets"]["lite"]["sha256"] is None and "bootloader" not in str(r["assets"])


def test_update_only_when_newer_and_for_the_variant():
    rel = su.parse_releases(RELEASES)
    assert su.update_for({"fw": "1.6.0", "var": "full"}, rel) == \
        {"version": "1.6.1", "url": "https://x/r/v1.6.1", "size": 1507681, "file": "idtelemetry-full-1.6.1.bin"}
    assert su.update_for({"fw": "1.6.0", "var": "lite"}, rel)["file"] == "idtelemetry-lite-1.6.1.bin"
    assert su.update_for({"fw": "1.6.1", "var": "full"}, rel) is None
    assert su.update_for({"fw": "1.10.0", "var": "full"}, rel) is None     # numeric, not text order
    assert su.update_for(None, rel) is None and su.update_for({"fw": "x"}, rel) is None


def test_switched_off_makes_no_request(monkeypatch):
    calls = []
    monkeypatch.setattr(su.config, "STICK_UPDATE_CHECK", False)
    monkeypatch.setattr(su, "_fetch", lambda: calls.append(1))
    assert su.latest() is None and calls == []
