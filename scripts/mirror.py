#!/usr/bin/env python3
"""GitHub -> GitLab mirror. Stdlib + git only.

TARGET_REPO set   -> mirror just that "owner/repo" (webhook-triggered)
TARGET_REPO empty -> full reconcile of every repo the GH_PAT can see
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def env_bool(name, default):
    v = (os.environ.get(name) or "").strip().lower()
    return default if not v else v in ("1", "true", "yes")


GH_PAT = os.environ["GH_PAT"]
GL_PAT = os.environ["GITLAB_PAT"]
GL_URL = (os.environ.get("GITLAB_URL") or "https://gitlab.com").rstrip("/")
GL_ROOT = (os.environ.get("GITLAB_GROUP") or "mirror").strip("/")
TARGET = (os.environ.get("TARGET_REPO") or "").strip()
INCLUDE_FORKS = env_bool("INCLUDE_FORKS", True)
INCLUDE_ARCHIVED = env_bool("INCLUDE_ARCHIVED", True)
WORKERS = int(os.environ.get("WORKERS") or 4)

SPECS = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]


def b64(user, token):
    return base64.b64encode(f"{user}:{token}".encode()).decode()


GH_B64 = b64("x-access-token", GH_PAT)
GL_B64 = b64("oauth2", GL_PAT)
GH_CFG = f"http.https://github.com/.extraheader=Authorization: basic {GH_B64}"
GL_CFG = f"http.{GL_URL}/.extraheader=Authorization: basic {GL_B64}"
SECRETS = [GH_PAT, GL_PAT, GH_B64, GL_B64]

# make sure derived values never leak into Actions logs
for s in (GH_B64, GL_B64):
    print(f"::add-mask::{s}")


def scrub(text):
    text = str(text)
    for s in SECRETS:
        text = text.replace(s, "***")
    return text


def q(s):
    return urllib.parse.quote(s, safe="")


# ---------------------------------------------------------------- HTTP / APIs
def http(method, url, headers, data=None):
    h = dict(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, raw.decode(errors="replace")


def gh(method, path, data=None):
    return http(
        method,
        "https://api.github.com" + path,
        {
            "Authorization": f"Bearer {GH_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mirror-hub",
        },
        data,
    )


def gl(method, path, data=None):
    return http(method, f"{GL_URL}/api/v4{path}", {"PRIVATE-TOKEN": GL_PAT}, data)


def list_repos():
    repos, page = [], 1
    while True:
        st, data = gh(
            "GET",
            f"/user/repos?affiliation=owner,organization_member&per_page=100&page={page}",
        )
        if st != 200:
            raise RuntimeError(f"GitHub list repos failed: {st} {data}")
        repos += data
        if len(data) < 100:
            return repos
        page += 1


# ---------------------------------------------------------------------- git
def git(*args, cwd=None, cfg=()):
    cmd = ["git"]
    for c in cfg:
        cmd += ["-c", c]
    cmd += list(args)
    # run outside any repo so checkout's own credentials never get mixed in
    r = subprocess.run(
        cmd,
        cwd=cwd or tempfile.gettempdir(),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {scrub(r.stderr.strip())[-600:]}")
    return r.stdout


def ls_remote(url, cfg):
    out = git("ls-remote", "--heads", "--tags", url, cfg=[cfg])
    refs = {}
    for line in out.splitlines():
        sha, ref = line.split("\t")
        if not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


# ------------------------------------------------------- white / black lists
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm_repo(s):
    """'https://github.com/Owner/Repo.git' / 'owner/repo' -> 'owner/repo'."""
    s = re.sub(r"[?#].*$", "", str(s).strip())
    s = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", s, flags=re.I)
    s = s.strip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    return s.lower()


def load_json(name):
    """Parsed JSON from <repo root>/<name>; None if missing or empty."""
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as e:
        sys.exit(f"{name}: invalid JSON ({e})")


def repo_set(data, name):
    if data is None:
        return set()
    items = data.get("repos", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        sys.exit(f"{name}: 'repos' must be a list")
    return {norm_repo(x) for x in items if isinstance(x, str) and x.strip()}


def is_on(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("on", "true", "yes", "1")


def apply_lists(repos):
    """white-list.json (only_white_list = on) wins over everything.
    Otherwise black-list.json repos are skipped. Missing/empty files = no filter."""
    wl = load_json("white-list.json")
    if isinstance(wl, dict) and is_on(wl.get("only_white_list")):
        allow = repo_set(wl, "white-list.json")
        print(f"white-list: ON - only {len(allow)} listed repo(s) will be mirrored (black-list ignored)")
        if not allow:
            print("white-list: list is empty, nothing will be mirrored")
        return [r for r in repos if r["full_name"].lower() in allow]

    deny = repo_set(load_json("black-list.json"), "black-list.json")
    if not deny:
        return repos
    kept = []
    for r in repos:
        if r["full_name"].lower() in deny:
            print(f"black-list: skipping {r['full_name']}")
        else:
            kept.append(r)
    return kept


# ------------------------------------------------------------ GitLab layout
def slug(s):
    s = re.sub(r"[^A-Za-z0-9_.-]", "-", s)
    if s[0] in "-.":
        s = "_" + s[1:]
    if s.endswith((".git", ".atom")):
        s += "-repo"
    return s


_lock = threading.Lock()
_groups = {}


def group_id(owner):
    """GitLab subgroup <GL_ROOT>/<owner>, created on demand."""
    key = slug(owner)
    with _lock:
        if key in _groups:
            return _groups[key]
        st, g = gl("GET", f"/groups/{q(GL_ROOT + '/' + key)}")
        if st == 404:
            st, root = gl("GET", f"/groups/{q(GL_ROOT)}")
            if st != 200:
                raise RuntimeError(f"root group '{GL_ROOT}' not accessible ({st})")
            st, g = gl(
                "POST",
                "/groups",
                {"name": owner, "path": key, "parent_id": root["id"], "visibility": "private"},
            )
        if st not in (200, 201):
            raise RuntimeError(f"group {key}: {st} {g}")
        _groups[key] = g["id"]
        return g["id"]


def ensure_project(r):
    owner, name = r["owner"]["login"], r["name"]
    path = f"{GL_ROOT}/{slug(owner)}/{slug(name)}"
    st, p = gl("GET", f"/projects/{q(path)}")
    if st == 200:
        return p, False
    if st != 404:
        raise RuntimeError(f"project lookup {path}: {st} {p}")
    st, p = gl(
        "POST",
        "/projects",
        {
            "name": slug(name),
            "path": slug(name),
            "namespace_id": group_id(owner),
            "visibility": "private",
            "description": f"Mirror of https://github.com/{r['full_name']}",
        },
    )
    if st not in (200, 201):
        raise RuntimeError(f"project create {path}: {st} {p}")
    return p, True


def unprotect(pid):
    """Protected branches reject force-push; a mirror doesn't need protection."""
    st, lst = gl("GET", f"/projects/{pid}/protected_branches?per_page=100")
    if st == 200:
        for b in lst:
            gl("DELETE", f"/projects/{pid}/protected_branches/{q(b['name'])}")


def set_default(p, r, src):
    want = r.get("default_branch")
    if want and f"refs/heads/{want}" in src and p.get("default_branch") != want:
        gl("PUT", f"/projects/{p['id']}", {"default_branch": want})


# --------------------------------------------------------------------- sync
def sync(r):
    full = r["full_name"]
    p, _created = ensure_project(r)  # new/empty repos still get a GitLab project
    gh_url = f"https://github.com/{full}.git"
    gl_url = f"{GL_URL}/{p['path_with_namespace']}.git"

    src = ls_remote(gh_url, GH_CFG)
    if not src:
        return "empty"
    dst = ls_remote(gl_url, GL_CFG)

    if src == dst:
        set_default(p, r, src)
        return "skipped"

    unprotect(p["id"])
    with tempfile.TemporaryDirectory() as d:
        git("init", "--bare", "-q", d)
        # only heads + tags (skips refs/pull/*, which GitLab rejects)
        git("fetch", "-q", "--no-tags", gh_url, *SPECS, cwd=d, cfg=[GH_CFG])
        # LFS: objects must exist on GitLab BEFORE the refs are pushed,
        # otherwise its pre-receive hook rejects the push
        # GitLab's LFS upload step sends its own Authorization header, which clashes
        # with http.extraheader ("duplicate header"), so LFS push to GitLab
        # authenticates through the URL instead.
        parts = urllib.parse.urlsplit(GL_URL)
        gl_auth_url = (
            f"{parts.scheme}://oauth2:{q(GL_PAT)}@{parts.netloc}"
            f"{parts.path}/{p['path_with_namespace']}.git"
        )
        git("remote", "add", "gh", gh_url, cwd=d)
        git("remote", "add", "gl", gl_auth_url, cwd=d)
        git("config", "lfs.locksverify", "false", cwd=d)
        git("lfs", "fetch", "--all", "gh", cwd=d, cfg=[GH_CFG])
        git("lfs", "push", "--all", "gl", cwd=d)
        git("push", "-q", gl_url, *SPECS, cwd=d, cfg=[GL_CFG])
        set_default(p, r, src)  # before prune: old default can't be deleted
        stale = [ref for ref in dst if ref not in src]
        for i in range(0, len(stale), 50):
            git("push", "-q", gl_url, *[f":{x}" for x in stale[i : i + 50]], cwd=d, cfg=[GL_CFG])
    return "synced"


def run_one(r):
    full = r["full_name"]
    for attempt in (1, 2):
        try:
            return full, sync(r), ""
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return full, "failed", scrub(e)
            time.sleep(5)


def preflight():
    """Fail fast with a clear message if the GitLab token/group is wrong."""
    print(f"GitLab: {GL_URL}, root group: '{GL_ROOT}'")
    st, me = gl("GET", "/user")
    if st != 200:
        sys.exit(
            f"GitLab token rejected ({st}). Use a CLASSIC personal access token "
            "with scopes api + write_repository."
        )
    print(f"GitLab user: {me.get('username')}")
    st, g = gl("GET", f"/groups/{q(GL_ROOT)}")
    if st == 200:
        print(f"GitLab root group OK: {g['full_path']}")
        return
    st2, lst = gl("GET", "/groups?min_access_level=30&per_page=50")
    names = (
        ", ".join(x["full_path"] for x in lst)
        if st2 == 200 and isinstance(lst, list) and lst
        else "(none found)"
    )
    sys.exit(
        f"GitLab group '{GL_ROOT}' not found ({st}). "
        f"Groups this token can access: {names}"
    )


def main():
    preflight()
    if TARGET:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", TARGET):
            sys.exit(f"invalid TARGET_REPO: {TARGET!r}")
        st, r = gh("GET", f"/repos/{TARGET}")
        if st == 404:
            print(f"{TARGET}: not found / no access (deleted?) - nothing to do")
            return 0
        if st != 200:
            sys.exit(f"GitHub lookup failed: {st} {r}")
        repos = [r]
    else:
        repos = list_repos()

    repos = apply_lists(repos)
    repos = [
        r
        for r in repos
        if (INCLUDE_FORKS or not r["fork"]) and (INCLUDE_ARCHIVED or not r["archived"])
    ]
    print(f"{len(repos)} repo(s) to process")

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(run_one, repos))

    counts = {}
    lines = []
    for full, status, err in sorted(results):
        counts[status] = counts.get(status, 0) + 1
        line = f"{status:8} {full}" + (f"  -> {err}" if err else "")
        lines.append(line)
        print(line)

    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to do"
    print(f"\nSummary: {summary}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(f"### Mirror result\n{summary}\n\n```\n" + "\n".join(lines) + "\n```\n")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
