"""Keep a running count of MJSNet downloads.

GitHub counts downloads per release file, but publish.py replaces each file in the
public downloads repo's ``latest`` release on every version, which starts that
file's count again at 0. This script takes a snapshot of the counts and remembers
the last count of every file that has been replaced, so the totals keep growing
across versions.

It runs in the downloads repo (msanty573/OmniNet-Downloads) as the "Download
stats" GitHub Actions workflow: every hour, and whenever publish.py starts (so the
last snapshot before a file is replaced is only minutes old). It works in a
checkout of the repo's ``stats`` branch and writes there:

    state.json      what it needs next time (each file's id and count, retired counts,
                    and "excluded": our own test downloads, edited by hand)
    downloads.json  the public summary, for a download counter on the website:
                    https://raw.githubusercontent.com/msanty573/OmniNet-Downloads/stats/downloads.json

    python download_stats.py <stats-folder>       (GITHUB_TOKEN optional; raises the rate limit)

Standard library only. The source lives in the private MJSNet repo
(packaging/download_stats.py, packaging/downloads-stats.yml) and is copied to the
downloads repo by hand, like its README and SECURITY.md.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("STATS_REPO", "msanty573/OmniNet-Downloads")
TAG = "latest"
HISTORY_DAYS = 3660


def platform_of(name: str) -> str:
    n = name.lower()
    if "macos" in n:
        return "macOS"
    if "windows" in n or "setup" in n:
        return "Windows"
    return "Linux"


def kind_of(name: str) -> str:
    """'update' for the Check for Updates files, 'download' for everything people install."""
    return "update" if name.startswith("update-") and name.endswith(".json") else "download"


def versions_in(body: str) -> dict[str, str]:
    """File -> version, from the release's File | Version table."""
    return dict(re.findall(r"^\| `([^`]+)` \| ([^|]+?) \|$", body or "", re.M))


def fetch_release(repo: str = REPO, tag: str = TAG) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "mjsnet-download-stats"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def update_state(state: dict, release: dict, now: dt.datetime) -> dict:
    """Fold one snapshot of the release into ``state`` (returned; the input is copied)."""
    state = json.loads(json.dumps(state or {}))
    seen = state.setdefault("assets", {})
    retired = state.setdefault("retired", {})
    state.setdefault("since", now.date().isoformat())
    versions = versions_in(release.get("body", ""))
    for a in release.get("assets", []):
        name, count = a["name"], int(a["download_count"])
        old = seen.get(name)
        if old and old["id"] != a["id"]:
            # The file was replaced: keep its last count under the version it was.
            key = old.get("version") or "unknown"
            bucket = retired.setdefault(name, {})
            bucket[key] = bucket.get(key, 0) + old["count"]
        version = versions.get(name)
        if kind_of(name) == "update":
            # update-<key>.json carries its own version; use the table's newest as a guide.
            version = (old or {}).get("version") if old and old["id"] == a["id"] else None
            version = version or max(versions.values(), default=None, key=_vkey)
        seen[name] = {"id": a["id"], "count": count, "version": version,
                      "uploaded": a.get("updated_at") or a.get("created_at")}
    s = summarize(state, now)
    day = now.date().isoformat()
    hist = [h for h in state.get("history", []) if h["date"] != day]
    hist.append({"date": day, "downloads": s["downloads"], "update_checks": s["update_checks"]})
    state["history"] = hist[-HISTORY_DAYS:]
    state["updated"] = now.isoformat(timespec="seconds")
    return state


def _vkey(v: str | None):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def summarize(state: dict, now: dt.datetime | None = None) -> dict:
    """The public summary: totals by kind, platform, file and version, plus daily history."""
    by_file: dict[str, int] = {}
    by_version: dict[str, int] = {}
    for name, info in state.get("assets", {}).items():
        by_file[name] = by_file.get(name, 0) + info["count"]
        if kind_of(name) == "download":
            v = info.get("version") or "unknown"
            by_version[v] = by_version.get(v, 0) + info["count"]
    for name, bucket in state.get("retired", {}).items():
        for v, c in bucket.items():
            by_file[name] = by_file.get(name, 0) + c
            if kind_of(name) == "download":
                by_version[v] = by_version.get(v, 0) + c
    # Our own test downloads (release checks), recorded by hand in state["excluded"]
    # as {file: {version: count}}, so the public numbers count only other people.
    for name, bucket in state.get("excluded", {}).items():
        for v, c in bucket.items():
            by_file[name] = max(0, by_file.get(name, 0) - c)
            if kind_of(name) == "download":
                by_version[v] = max(0, by_version.get(v, 0) - c)
    by_version = {v: c for v, c in by_version.items() if c}
    downloads = {n: c for n, c in by_file.items() if kind_of(n) == "download"}
    updates = {n: c for n, c in by_file.items() if kind_of(n) == "update"}
    by_platform: dict[str, int] = {}
    for n, c in downloads.items():
        by_platform[platform_of(n)] = by_platform.get(platform_of(n), 0) + c
    return {
        "updated": (now.isoformat(timespec="seconds") if now else state.get("updated")),
        "since": state.get("since"),
        "downloads": sum(downloads.values()),
        "by_platform": dict(sorted(by_platform.items())),
        "by_version": dict(sorted(by_version.items(), key=lambda kv: _vkey(kv[0]), reverse=True)),
        "by_file": dict(sorted(downloads.items(), key=lambda kv: kv[0].lower())),
        "update_checks": sum(updates.values()),
        "update_checks_by_file": dict(sorted(updates.items())),
        "note": ("downloads = installers and packages downloaded from the downloads page "
                 "(not installs from the dnf/apt repositories); update_checks = times an "
                 "installed copy checked for updates"),
        "history": state.get("history", []),
    }


def exclude_current(state: dict) -> dict:
    """Mark every download counted so far as ours (used once, when tracking starts)."""
    ex = state.setdefault("excluded", {})
    for name, info in state.get("assets", {}).items():
        bucket = ex.setdefault(name, {})
        v = info.get("version") or "unknown"
        bucket[v] = info["count"]
    return state


def main(folder: str) -> None:
    out = Path(folder)
    state_file = out / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        release = fetch_release()
    except urllib.error.HTTPError as e:
        if e.code == 404:              # no downloads release yet (a brand-new repo): nothing to count
            print("No downloads release yet; nothing to count.")
            return
        raise
    state = update_state(state, release, now)
    summary = summarize(state, now)
    old_file = out / "downloads.json"
    old = json.loads(old_file.read_text()) if old_file.exists() else {}
    if ({k: v for k, v in old.items() if k != "updated"}
            == {k: v for k, v in summary.items() if k != "updated"}
            and str(old.get("updated", ""))[:10] == now.date().isoformat()):
        print("No change since the last snapshot today.")
        return
    state_file.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    (out / "downloads.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"{summary['downloads']} downloads, {summary['update_checks']} update checks "
          f"(since {summary['since']}): {summary['by_platform']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
