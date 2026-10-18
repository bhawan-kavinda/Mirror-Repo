#!/usr/bin/env python3
"""Keep repos.md (the repo checklist) in sync with GitHub.

- Scheduled runs only proceed when timeloop.json says it is time.
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
def every_hours():
    """timeloop.json -> N, or None when the file is missing or empty."""
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
    n = data.get("every_hours") if isinstance(data, dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 24:
        sys.exit("timeloop.json: 'every_hours' must be a whole number from 1 to 24")
    return n


def is_due():
    """Scheduled runs only: due when the UTC hour is a multiple of every_hours.
    Manual and push runs are always due. Missing file = every tick is due."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    n = every_hours()
    if n is None:
        return True
    hour = datetime.now(timezone.utc).hour
    print(f"timeloop: every {n}h, current UTC hour is {hour}")
    return hour % n == 0


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
