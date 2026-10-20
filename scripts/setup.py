#!/usr/bin/env python3
"""Keep repos.md (the repo checklist) in sync with GitHub.

- Scheduled runs only proceed when timeloop.json says it is time (fixed "at" times and/or every_hours).
- repos.md missing -> create it with every repo the GH_PAT can see ([x] by default).
- repos.md exists  -> add repos that are not listed yet ([x] by default).
                      If there are none, the file is left untouched (no PR needed).
Existing marks are never changed. Stdlib only.

Writes due=true|false to $GITHUB_OUTPUT (false = skip everything, mirror included).
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GH_PAT = os.environ["GH_PAT"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "repos.md")
LINE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s+(\S+)(?:\s*\|\s*(.*?))?\s*$")

HEADER = """# Repositories to mirror

Edit the marks, then merge the PR. Every mirror run reads this file.

- `[x]` = mirror this repo to GitLab
- `[ ]` = skip this repo

Repos that are not listed here are skipped too.
The setup workflow opens a PR whenever it finds a repo that is not listed yet.
"""


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


# ------------------------------------------------------------------ timeloop
TIMELOOP_KEYS = {"every_hours", "at", "timezone"}
AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def read_timeloop():
    """timeloop.json -> (every_hours | None, set of 'at' hours, tzinfo, tz name).

    Returns None when the file is missing or empty (= every tick is due).
    Supported keys (at least one of every_hours / at is required):
      every_hours : 1..24   due when the local hour is a multiple of N
      at          : ["06:00", "18:00"]   due in those local hours (24h clock)
      timezone    : IANA name, default "UTC" (e.g. "Asia/Colombo")
    If both every_hours and at are set, a tick is due when either matches.
    """
    path = os.path.join(ROOT, "timeloop.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError as e:
        sys.exit(f"timeloop.json: invalid JSON ({e})")
    if not isinstance(data, dict):
        sys.exit("timeloop.json: must be a JSON object")

    unknown = sorted(set(data) - TIMELOOP_KEYS)
    if unknown:
        sys.exit(f"timeloop.json: unknown key(s) {unknown}; allowed: {sorted(TIMELOOP_KEYS)}")

    every = data.get("every_hours")
    if every is not None and (not isinstance(every, int) or isinstance(every, bool)
                              or not 1 <= every <= 24):
        sys.exit("timeloop.json: 'every_hours' must be a whole number from 1 to 24")

    at_hours = set()
    at = data.get("at")
    if at is not None:
        if not isinstance(at, list) or not at:
            sys.exit("timeloop.json: 'at' must be a non-empty list like [\"06:00\", \"18:00\"]")
        for item in at:
            m = AT_RE.match(item) if isinstance(item, str) else None
            if not m:
                sys.exit(f"timeloop.json: bad time {item!r} in 'at' - use 24h \"HH:MM\", e.g. \"06:00\"")
            at_hours.add(int(m.group(1)))

    if every is None and not at_hours:
        sys.exit("timeloop.json: set 'every_hours' (e.g. 6) or 'at' (e.g. [\"06:00\", \"18:00\"])")

    tz_name = data.get("timezone", "UTC")
    if not isinstance(tz_name, str):
        sys.exit("timeloop.json: 'timezone' must be a string like \"Asia/Colombo\"")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"timeloop.json: unknown timezone {tz_name!r} (use an IANA name such as \"Asia/Colombo\" or \"UTC\")")

    return every, at_hours, tz, tz_name


def is_due(now=None):
    """Scheduled runs only. Due when the current hour (in the configured timezone)
    is a multiple of every_hours, or is one of the 'at' hours.
    Manual and push runs are always due. Missing file = every tick is due.
    Only the hour matters: the run happens at the hourly tick inside that hour.
    The tick's local minute must sit well inside the hour (see the cron in setup.yml),
    otherwise a delayed scheduled run slips into the next hour and the slot is missed."""
    if now is None and os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    cfg = read_timeloop()
    if cfg is None:
        return True
    every, at_hours, tz, tz_name = cfg
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour = local.hour
    rules = []
    if every:
        rules.append(f"every {every}h")
    if at_hours:
        rules.append("at " + ", ".join(f"{h:02d}:00" for h in sorted(at_hours)))
    utc_now = local.astimezone(timezone.utc)
    print(f"timeloop: {' + '.join(rules)} ({tz_name}); local time now {local:%H:%M}, "
          f"hour {hour} (UTC {utc_now:%H:%M})")
    return bool((every and hour % every == 0) or hour in at_hours)


# ------------------------------------------------------------------- GitHub
def gh_get(path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {GH_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mirror-hub-setup",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API {path} failed: {e.code}")


def list_repos():
    # same affiliation as mirror.py, so both see the same repos
    repos, page = [], 1
    while True:
        data = gh_get(
            f"/user/repos?affiliation=owner,organization_member&per_page=100&page={page}"
        )
        repos += data
        if len(data) < 100:
            return repos
        page += 1


# ------------------------------------------------------------------ repos.md
def read_entries():
    """{'owner/repo' (lower): {name, mark, tags}} from the current repos.md."""
    entries = {}
    if os.path.isfile(OUT):
        with open(OUT, encoding="utf-8") as f:
            for line in f:
                m = LINE.match(line)
                if m:
                    key, mark = m.group(2).lower(), m.group(1) in ("x", "X")
                    if key in entries:
                        mark = mark and entries[key]["mark"]  # unticked wins on duplicates
                    entries[key] = {"name": m.group(2), "mark": mark, "tags": m.group(3) or ""}
    return entries


def tags_of(r):
    tags = ["private" if r["private"] else "public"]
    if r["fork"]:
        tags.append("fork")
    if r["archived"]:
        tags.append("archived")
    return ", ".join(tags)


def write_entries(entries):
    out = [HEADER]
    owner = None
    for e in sorted(entries.values(), key=lambda e: e["name"].lower()):
        o = e["name"].split("/")[0]
        if o.lower() != (owner or "").lower():
            owner = o
            out.append(f"\n## {owner}\n")
        tags = f" | {e['tags']}" if e["tags"] else ""
        out.append(f"- [{'x' if e['mark'] else ' '}] {e['name']}{tags}")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main():
    if not is_due():
        print("timeloop: not due this hour - nothing to do")
        set_output("due", "false")
        return
    set_output("due", "true")

    existed = os.path.isfile(OUT)
    entries = read_entries()
    repos = list_repos()
    new = [r for r in repos if r["full_name"].lower() not in entries]

    if existed and not new:
        print(f"repos.md: all {len(repos)} repo(s) are already listed - no changes")
        return

    for r in repos:
        key = r["full_name"].lower()
        if key in entries:
            entries[key].update(name=r["full_name"], tags=tags_of(r))  # mark is kept
        else:
            entries[key] = {"name": r["full_name"], "mark": True, "tags": tags_of(r)}
    write_entries(entries)

    if existed:
        print(f"repos.md: added {len(new)} new repo(s): " + ", ".join(r["full_name"] for r in new))
    else:
        print(f"repos.md: created with {len(new)} repo(s), all marked [x]")


if __name__ == "__main__":
    main()
