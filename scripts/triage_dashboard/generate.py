#!/usr/bin/env python3
"""Build the data for the ExtensionsIndex triage dashboard.

Reads open pull requests of the extensions index from the GitHub API, works out whose turn
each one is from GitHub's own signals (comments, reviews, pushes, assignees, review requests,
labels), looks at each extension's source repository, and writes data.json and version.json
for the static page in site/.

The extension source is only ever read as text (CMake files, Python files) and never run.

Standard library only. Authentication: GITHUB_TOKEN or GH_TOKEN, otherwise `gh auth token`.

Usage:
  generate.py --out _site [--repo Slicer/ExtensionsIndex] [--previous URL-or-path]
"""

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
BOT_LOGINS = {"github-actions", "copilot-pull-request-reviewer", "dependabot", "Copilot"}
# Maintainers listed in the page's "You are" menu even when they are not active on an open PR.
KNOWN_MAINTAINERS = ["lassoan", "jamesobutler", "pieper", "cpinter", "jcfr", "fedorov", "thewtex", "ebrahimebrahim"]
SOURCE_TTL = dt.timedelta(minutes=60)  # how often to re-check an extension's source repository
BASELINE_TTL = dt.timedelta(hours=24)
SHARED_PACKAGES = ["numpy", "scipy", "SimpleITK", "pydicom", "requests", "vtk", "torch"]
SCHEMA = 1
SCAN_VERSION = 3  # bump when scan_source changes, so cached scans are redone

TOKEN = None
MAINTAINERS = set()  # lower-case logins from maintainers.txt
STATS = {"rest": 0, "graphql": 0, "raw": 0}


# ----------------------------------------------------------------------------- GitHub access

def token():
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def request(url, data=None, accept="application/vnd.github+json", auth=True, raw=False, timeout=60):
    headers = {"Accept": accept, "User-Agent": "extensionsindex-triage-dashboard"}
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return payload.decode("utf-8", "replace") if raw else json.loads(payload.decode() or "null")
        except urllib.error.HTTPError as e:
            if e.code in (404, 409, 410, 422, 451):
                return None
            if e.code in (403, 429) and e.headers.get("Retry-After") and attempt < 3:
                time.sleep(min(int(e.headers["Retry-After"]), 120))
                continue
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def rest(path, **params):
    STATS["rest"] += 1
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return request(f"{API}/{path.lstrip('/')}{q}")


def graphql(query, **variables):
    STATS["graphql"] += 1
    r = request(f"{API}/graphql", {"query": query, "variables": variables})
    if r.get("errors"):
        raise RuntimeError(r["errors"])
    return r["data"]


def raw_file(owner, name, sha, path, limit=400_000):
    """Read one file of a GitHub repository as text (raw.githubusercontent.com is not API-rate-limited)."""
    STATS["raw"] += 1
    url = f"{RAW}/{owner}/{name}/{sha}/{urllib.parse.quote(path)}"
    try:
        text = request(url, auth=False, raw=True, accept="text/plain", timeout=30)
    except Exception:  # noqa: BLE001 - one unreadable file should not stop the scan
        return None
    return text[:limit] if text is not None else None


PR_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:15, after:$cursor, orderBy:{field:CREATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt isDraft headRefOid
        author { login url }
        labels(first:10) { nodes { name } }
        assignees(first:5) { nodes { login } }
        reviewRequests(first:5) { nodes { requestedReviewer { ... on User { login } ... on Team { slug } } } }
        files(first:20) { nodes { path changeType } }
        comments(last:40) { nodes { author { login } authorAssociation createdAt url body } }
        reviews(last:20) { nodes { author { login } authorAssociation state submittedAt url body } }
        commits(last:20) { nodes { commit { oid committedDate authoredDate messageHeadline
          author { user { login } } committer { user { login } }
          statusCheckRollup { contexts(first:40) { nodes {
            __typename
            ... on CheckRun { name conclusion status detailsUrl }
            ... on StatusContext { context state targetUrl }
          } } } } } }
      }
    }
  }
}"""

HISTORY_QUERY = """
query($q:String!, $cursor:String) {
  search(query:$q, type:ISSUE, first:100, after:$cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { createdAt mergedAt files(first:3) { totalCount nodes { path changeType } } } }
  }
}"""


# ----------------------------------------------------------------------------- helpers

def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def days(a, b):
    return (b - a).total_seconds() / 86400


def is_entry_path(path):
    return "/" not in path and (path.endswith(".json") or path.endswith(".s4ext") or "." not in path)


def entry_from_patch(patch):
    """Rebuild an added or modified catalog entry from a unified diff patch (new-side lines)."""
    lines = []
    for line in (patch or "").splitlines():
        if line.startswith(("@@", "-", "\\")):  # hunk headers, removed lines, "\ No newline at end of file"
            continue
        lines.append(line[1:] if line[:1] in "+ " else line)
    text = "\n".join(lines)
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        legacy = {}
        for key, field in (("scmurl", "scm_url"), ("scmrevision", "scm_revision")):
            m = re.search(rf"{key}\s+(\S+)", text)
            if m:
                legacy[field] = m.group(1)
        return legacy, text


def clean_text(text, limit=900):
    """Comment body as short plain text; notes whether it carried an AI-assistance disclaimer."""
    t = (text or "").replace("\r\n", "\n")
    ai = bool(re.search(r"^>.*\b(Claude|AI assistant)\b", t, re.M))
    t = re.sub(r"^>.*\b(Claude|AI assistant)\b.*$", "", t, flags=re.M)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"```.*?```", "[code]", t, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[image]", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"`|\*\*|__", "", t)  # code ticks and bold; single _ stays (identifiers like pip_install)
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    t = re.sub(r"^(\s*)\*\s+", r"\1- ", t, flags=re.M)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return {"text": t[:limit] + ("…" if len(t) > limit else ""), "ai": ai}


def check_summary(contexts):
    seen = {}
    for c in contexts:
        if c["__typename"] == "CheckRun":
            seen[c["name"]] = {"name": c["name"], "state": (c.get("conclusion") or c.get("status") or "").upper(),
                               "url": c.get("detailsUrl")}
        else:
            seen[c["context"]] = {"name": c["context"], "state": (c.get("state") or "").upper(), "url": c.get("targetUrl")}
    return sorted(seen.values(), key=lambda c: c["name"])


# ----------------------------------------------------------------------------- extension source

def source_info(scm_url, revision):
    """Latest commit on the tracked revision, license, size."""
    info = {"url": None, "host": None, "error": None, "checked_at": iso(now_utc())}
    if not scm_url:
        info["error"] = "No scm_url in the entry"
        return info
    web = re.sub(r"\.git$", "", scm_url.strip())
    info["url"] = web
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)/?$", web)
    if m:
        owner, name = m.group(1), m.group(2)
        repo = rest(f"repos/{owner}/{name}")
        if not repo:
            info["error"] = "Repository not found"
            return info
        info.update(host="github", owner=repo["owner"]["login"], name=repo["name"], full_name=repo["full_name"],
                    html=repo["html_url"], license=(repo.get("license") or {}).get("spdx_id"),
                    size_mb=round(repo.get("size", 0) / 1024, 1), pushed_at=repo.get("pushed_at"),
                    default_branch=repo.get("default_branch"), archived=repo.get("archived"))
        rev = revision or repo.get("default_branch")
        c = rest(f"repos/{owner}/{name}/commits/{urllib.parse.quote(rev, safe='')}")
        if c:
            info.update(rev=rev, head_sha=c["sha"], head_date=c["commit"]["committer"]["date"],
                        head_msg=c["commit"]["message"].splitlines()[0][:200], head_url=c["html_url"])
        else:
            info["error"] = f"Revision {rev!r} not found"
        return info
    m = re.match(r"https?://([^/]+)/(.+?)/?$", web)
    if m:  # GitLab-style host (e.g. gitlab.kitware.com)
        host, path = m.group(1), m.group(2)
        info.update(host=host, html=web)
        try:
            cs = request(f"https://{host}/api/v4/projects/{urllib.parse.quote(path, safe='')}/repository/commits?" +
                         urllib.parse.urlencode({"ref_name": revision or "", "per_page": 1}), auth=False)
            if cs:
                info.update(rev=revision, head_sha=cs[0]["id"], head_date=cs[0]["committed_date"],
                            head_msg=cs[0]["title"][:200], head_url=cs[0].get("web_url"))
        except Exception as e:  # noqa: BLE001 - best effort for non-GitHub hosts
            info["error"] = f"Could not query {host}: {e}"
    return info


CMAKE_SET = re.compile(r'\bset\s*\(\s*([A-Za-z_]\w*)\s+((?:"(?:[^"\\]|\\.)*"|[^)"])*)\)', re.S)
SCRIPTED_MACRO = re.compile(r"slicerMacroBuildScripted(?:Module|CLI)\s*\((.*?)\)", re.S)
MACRO_KEYWORDS = {"NAME", "SCRIPTS", "RESOURCES", "WITH_GENERIC_TESTS", "WITH_SUBDIR", "TARGET_NAME", "DESTINATION_DIR",
                  "VERBOSE", "NO_INSTALL_SUBDIR", "PYTHON_SCRIPTS"}
PIP_CALL = re.compile(r"""(?:pip_install|pip_install_wrapper|installPackage|install_package)\s*\(\s*f?["']([^"']{1,160})""")
PIP_CMD = re.compile(r"""pip["']?\s*,\s*["']install["']\s*,\s*(?:["'][^"']*["']\s*,\s*)*?f?["']([^"'-][^"']{0,160})"""
                     r"""|["'](?:python3?\s+-m\s+)?pip3?\s+install\s+([^"'\n]{1,160})["']""")
PIN = re.compile(r"(?<![\w-])(" + "|".join(SHARED_PACKAGES) + r")\s*(==|<=|<|~=)\s*[\d.*]+", re.I)
QT_BUNDLERS = re.compile(r"(?<![\w-])(opencv-python|opencv-contrib-python|PyQt[56]|PySide[26]|pyqt[56]|pyside[26])(?![\w-])")
QT_IMPORT = re.compile(r"^\s*(?:from|import)\s+(PyQt[56]|PySide[26])\b", re.M)
SKIP_DIRS = re.compile(r"(^|/)(testing|tests?|docs?|examples?)(/|$)", re.I)


def cmake_sets(text):
    """All set(NAME value) assignments in a CMake file (later ones win), comments removed."""
    out = {}
    for name, value in CMAKE_SET.findall(re.sub(r"#[^\n]*", "", text or "")):
        out[name] = value.strip()
    return out


def unquote_cmake(v):
    v = (v or "").strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def expand(value, variables):
    for _ in range(4):
        new = re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def module_scripts(cmake_text):
    """Python files a scripted-module CMakeLists.txt installs, or None if they cannot be read off the file."""
    if re.search(r"file\s*\(\s*GLOB", cmake_text, re.I):
        return None
    clean = re.sub(r"#[^\n]*", "", cmake_text)
    m = SCRIPTED_MACRO.search(clean)
    if not m:
        return None
    variables = {k: unquote_cmake(v) for k, v in cmake_sets(clean).items()}
    tokens = re.split(r"[\s;]+", m.group(1))
    scripts, collecting = [], False
    for tok in tokens:
        tok = tok.strip('"')
        if tok in MACRO_KEYWORDS:
            collecting = tok in ("SCRIPTS", "PYTHON_SCRIPTS")
            continue
        if collecting and tok:
            scripts += [t.strip('"') for t in re.split(r"[\s;]+", expand(tok, variables)) if t.strip('"')]
    if any("${" in s for s in scripts):
        return None
    return [s for s in scripts if s.endswith(".py")]


def scan_source(src):
    """Read the extension's source as text and collect mechanical findings. Never runs anything."""
    owner, name, sha = src["owner"], src["name"], src["head_sha"]
    scan = {"sha": sha, "version": SCAN_VERSION, "hints": [], "cmake": {}, "py_files": 0, "modules": []}
    tree = rest(f"repos/{owner}/{name}/git/trees/{sha}", recursive=1)
    if not tree:
        scan["hints"].append({"level": "warn", "text": "Could not list the repository files."})
        return scan
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    if tree.get("truncated"):
        scan["hints"].append({"level": "warn", "text": "Repository is very large; the file list was truncated."})
    scan["has_license_file"] = any(re.match(r"(?i)^(license|licence|copying)(\.\w+)?$", p) for p in paths)

    def read(p):
        return p, raw_file(owner, name, sha, p)

    top = raw_file(owner, name, sha, "CMakeLists.txt")
    if top is None:
        scan["hints"].append({"level": "bad", "text": "No top-level CMakeLists.txt."})
        return scan
    sets = {k: unquote_cmake(v) for k, v in cmake_sets(top).items()}
    scan["cmake"] = {k[len("EXTENSION_"):].lower(): v for k, v in sets.items()
                     if k in ("EXTENSION_HOMEPAGE", "EXTENSION_CATEGORY", "EXTENSION_CONTRIBUTORS", "EXTENSION_DESCRIPTION",
                              "EXTENSION_ICONURL", "EXTENSION_SCREENSHOTURLS", "EXTENSION_DEPENDS", "EXTENSION_STATUS")}
    m = re.search(r"project\s*\(\s*([\w.-]+)", top, re.I)
    scan["cmake"]["project"] = m.group(1) if m else None

    py_all = [p for p in paths if p.endswith(".py")]
    scan["py_files"] = len(py_all)
    module_dirs = sorted({p.rsplit("/", 1)[0] for p in paths if p.endswith("/CMakeLists.txt") and not SKIP_DIRS.search(p)})[:40]
    readable = [p for p in py_all if not SKIP_DIRS.search(p)][:120]
    readable += [p for p in paths if re.search(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.py)$", p)][:6]
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        cmakes = dict(ex.map(read, [d + "/CMakeLists.txt" for d in module_dirs]))
        files = dict(ex.map(read, readable))

    # Scripted modules: every Python file the module imports must be listed for installation.
    for d in module_dirs:
        t = cmakes.get(d + "/CMakeLists.txt") or ""
        if not re.search(r"slicerMacroBuildScripted(Module|CLI)", t):
            continue
        listed = module_scripts(t)
        mod = re.search(r"NAME\s+\"?([\w.-]+)", SCRIPTED_MACRO.search(t).group(1)) if SCRIPTED_MACRO.search(t) else None
        info = {"dir": d, "name": expand(mod.group(1), {k: unquote_cmake(v) for k, v in cmake_sets(t).items()}) if mod else d.rsplit("/", 1)[-1]}
        if listed is None:
            info["glob"] = True
            scan["modules"].append(info)
            continue
        info["listed"] = len(listed)
        here = [p[len(d) + 1:] for p in py_all if p.startswith(d + "/") and not SKIP_DIRS.search(p[len(d) + 1:])]
        missing = [x for x in listed if x not in here]
        unlisted = [x for x in here if x not in listed]
        listed_text = "\n".join(files.get(f"{d}/{x}") or "" for x in listed if x in here)
        imported = []
        for x in unlisted:
            parts = x[:-3].split("/")
            names = {parts[-1]} | ({parts[0]} if len(parts) > 1 else set())
            names.discard("__init__")
            if any(re.search(rf"^\s*(?:from|import)\s+\.?{re.escape(n)}\b", listed_text, re.M) for n in names):
                imported.append(x)
        if missing:
            info["missing"] = missing
            scan["hints"].append({"level": "bad", "path": d + "/CMakeLists.txt",
                                  "text": f"{d}/CMakeLists.txt installs files that do not exist: {', '.join(missing[:4])}"})
        if unlisted:
            info["unlisted"] = unlisted
        if imported and re.search(r"\binstall\s*\(\s*(DIRECTORY|FILES|PROGRAMS)\b", t, re.I):
            scan["hints"].append({"level": "info", "path": d + "/CMakeLists.txt",
                                  "text": f"{d}/ imports {len(imported)} Python file(s) that are not in its SCRIPTS list; its CMakeLists.txt has its own install() rules, so check they cover them."})
        elif imported:
            scan["hints"].append({"level": "bad", "path": d + "/CMakeLists.txt",
                                  "text": f"{d}/ imports {len(imported)} Python file(s) that CMake does not install, so the packaged module fails to load: {', '.join(imported[:4])}"})
        elif unlisted:
            scan["hints"].append({"level": "info", "path": d + "/CMakeLists.txt",
                                  "text": f"{len(unlisted)} Python file(s) in {d}/ are not installed (fine if nothing imports them): {', '.join(unlisted[:3])}"})
        scan["modules"].append(info)

    # What gets installed: pip calls in Python files, and requirements files.
    specs, uses_torch, uses_pytorchutils, qt_imports = [], False, False, []
    for p, t in files.items():
        if not t:
            continue
        if p.endswith(".py"):
            for s in PIP_CALL.findall(t) + [m.group(1) or m.group(2) for m in PIP_CMD.finditer(t)]:
                specs.append((p, s.strip()))
            if re.search(r"^\s*(import torch\b|from torch\b)", t, re.M):
                uses_torch = True
            if "PyTorchUtils" in t:
                uses_pytorchutils = True
            qt_imports += [(p, m) for m in QT_IMPORT.findall(t)]
        else:
            for line in t.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    specs.append((p, line))
    scan["pip"] = [{"path": p, "spec": s} for p, s in specs[:15]]
    scan["uses_torch"], scan["uses_pytorchutils"] = uses_torch, uses_pytorchutils
    pins = sorted({(p, m.group(0)) for p, s in specs for m in PIN.finditer(s)})
    if pins:
        scan["hints"].append({"level": "bad", "path": pins[0][0],
                              "text": "Pins or caps a package other extensions share: " + ", ".join(sorted({x for _, x in pins})[:5])})
    bundlers = sorted({(p, m.group(1)) for p, s in specs for m in QT_BUNDLERS.finditer(s)})
    if bundlers:
        scan["hints"].append({"level": "bad", "path": bundlers[0][0],
                              "text": "Installs a package that brings its own Qt: " + ", ".join(sorted({x for _, x in bundlers})[:4]) +
                                      " (use opencv-python-headless; Slicer already provides Qt)"})
    if qt_imports:
        scan["hints"].append({"level": "warn", "path": qt_imports[0][0],
                              "text": f"Imports {qt_imports[0][1]}; Slicer's Qt is used through PythonQt (import qt), so this fails or loads a second Qt."})
    if uses_torch and not uses_pytorchutils:
        scan["hints"].append({"level": "bad", "text": "Imports torch without going through the PyTorch extension (PyTorchUtils)."})
    py_specs = [s for s in specs if s[0].endswith(".py")]
    if py_specs:
        scan["hints"].append({"level": "info", "path": py_specs[0][0],
                              "text": f"{len(py_specs)} pip install call(s); check they ask first and do not run at startup."})

    icon = scan["cmake"].get("iconurl")
    if icon and icon.startswith("https://"):
        scan["icon_status"] = url_status(icon)
        if scan["icon_status"] != 200:
            scan["hints"].append({"level": "warn", "text": f"Icon URL does not load (HTTP {scan['icon_status']})."})
    elif not icon:
        scan["hints"].append({"level": "warn", "text": "No EXTENSION_ICONURL."})
    return scan


def url_status(url):
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "extensionsindex-triage-dashboard", "Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 if r.status in (200, 206) else r.status
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405):
                continue
            return e.code
        except Exception:  # noqa: BLE001
            return 0
    return 0


def entry_checks(entry, src, scan):
    """Findings that combine the catalog entry with the source (cheap, recomputed every run)."""
    hints = []
    if src.get("error"):
        hints.append({"level": "bad", "text": src["error"]})
    if src.get("host") == "github":
        if src.get("size_mb", 0) > 100:
            hints.append({"level": "bad", "text": f"Repository is {src['size_mb']:.0f} MB, over the index's 100 MB limit."})
        if not src.get("license") and not (scan or {}).get("has_license_file"):
            hints.append({"level": "bad", "text": "No license found."})
        if src.get("archived"):
            hints.append({"level": "bad", "text": "Repository is archived."})
    if scan and scan.get("cmake"):
        depends = scan["cmake"].get("depends") or ""
        cm = {d for d in re.split(r"[\s;]+", depends) if d and d.upper() != "NA"}
        js = set(entry.get("build_dependencies") or [])
        if cm != js:
            hints.append({"level": "bad", "text": f"Dependencies disagree: EXTENSION_DEPENDS has {sorted(cm) or 'none'}, "
                                                  f"the entry's build_dependencies has {sorted(js) or 'none'}."})
        if scan.get("uses_pytorchutils") and "PyTorch" not in (cm | js):
            hints.append({"level": "bad", "text": "Uses PyTorchUtils but does not declare the PyTorch extension as a dependency."})
    return hints


# ----------------------------------------------------------------------------- per-PR analysis

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def analyze(pr, pending_runs, src):
    author = pr["author"]["login"] if pr["author"] else "ghost"

    def role(login, assoc):
        if login == author:
            return "contributor"
        return "maintainer" if assoc in MAINTAINER_ASSOCIATIONS or login.lower() in MAINTAINERS else "other"

    events = []
    for c in pr["comments"]["nodes"]:
        login = (c["author"] or {}).get("login", "ghost")
        if login in BOT_LOGINS or login.endswith("[bot]"):
            continue
        events.append({"t": parse_time(c["createdAt"]), "who": login, "role": role(login, c["authorAssociation"]),
                       "kind": "comment", "url": c["url"], **clean_text(c["body"])})
    reviews = []
    for r in pr["reviews"]["nodes"]:
        login = (r["author"] or {}).get("login", "ghost")
        if login in BOT_LOGINS or login.endswith("[bot]") or not r["submittedAt"]:
            continue
        rl = role(login, r["authorAssociation"])
        reviews.append((parse_time(r["submittedAt"]), login, rl, r["state"]))
        kind = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "DISMISSED": "review dismissed"}.get(r["state"], "review")
        if r["state"] == "COMMENTED" and not (r["body"] or "").strip():
            continue  # container for inline comments; the inline comments themselves are not fetched
        events.append({"t": parse_time(r["submittedAt"]), "who": login, "role": rl, "kind": kind, "url": r["url"],
                       **clean_text(r["body"])})
    commits = [n["commit"] for n in pr["commits"]["nodes"]]
    rebases = {}
    last_push = None
    for k in commits:
        who = ((k.get("author") or {}).get("user") or {}).get("login") or author
        t = parse_time(k["authoredDate"])
        events.append({"t": t, "who": who, "role": "contributor" if who == author else "other", "kind": "commit",
                       "url": f"{pr['url']}/commits/{k['oid']}", "text": k["messageHeadline"], "ai": False})
        last_push = max(last_push or t, parse_time(k["committedDate"]))
        committer = ((k.get("committer") or {}).get("user") or {}).get("login")
        if committer and committer not in (author, "web-flow") and k["committedDate"] != k["authoredDate"]:
            rebases.setdefault((committer, k["committedDate"]), f"{pr['url']}/commits/{k['oid']}")
    for (who, t), url in rebases.items():  # branch updated by someone else (usually a maintainer)
        events.append({"t": parse_time(t), "who": who, "role": "maintainer", "kind": "updated branch", "url": url,
                       "text": "", "ai": False})
    events.sort(key=lambda e: e["t"])

    created = parse_time(pr["createdAt"])
    m_events = [e for e in events if e["role"] == "maintainer" and e["kind"] != "updated branch"]
    c_events = [e for e in events if e["role"] == "contributor"]
    last_m = m_events[-1] if m_events else None
    last_c = c_events[-1] if c_events else None
    last_c_t = last_c["t"] if last_c else created
    labels = [l["name"] for l in pr["labels"]["nodes"]]

    # Source pushes fire no event on the PR; count them as a contributor update.
    source_update = None
    if last_m and src.get("head_date") and parse_time(src["head_date"]) > max(last_m["t"], last_c_t):
        source_update = parse_time(src["head_date"])

    if pr["isDraft"]:
        turn, reason, since = "contributor", "Draft: the contributor has not marked it ready", last_c_t
    elif "status:on-hold" in labels:
        turn, reason, since = "hold", "Labeled on hold", (last_m or {}).get("t") or created
    elif last_m is None:
        turn, reason, since = "maintainers", "No maintainer has replied yet", created
    elif source_update:
        turn, reason, since = "maintainers", "New commits in the extension's source since the last maintainer comment", source_update
    elif last_c_t > last_m["t"]:
        turn, reason, since = "maintainers", "The contributor replied or pushed since the last maintainer comment", last_c_t
    else:
        turn, reason, since = "contributor", "Waiting on the contributor", last_m["t"]

    # Review decisions: the latest decisive review per maintainer.
    decision = {}
    for t, login, rl, state in sorted(reviews):
        if rl == "maintainer" and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            decision[login] = (state, t)
    approved = {l: t for l, (s, t) in decision.items() if s == "APPROVED"}
    changes = [l for l, (s, _) in decision.items() if s == "CHANGES_REQUESTED"]

    contexts = (commits[-1]["statusCheckRollup"] or {}).get("contexts", {}).get("nodes", []) if commits and commits[-1]["statusCheckRollup"] else []
    checks = check_summary(contexts)
    failing = [c for c in checks if c["state"] in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED")
               and not c["name"].startswith("ai-analysis")]
    validation_ran = any(c["name"].startswith("validate-extensions") for c in checks)
    runs = pending_runs.get(pr["headRefOid"], [])

    assignees = [n["login"] for n in pr["assignees"]["nodes"]]
    requested = [((n["requestedReviewer"] or {}).get("login")) for n in pr["reviewRequests"]["nodes"]]
    requested = [r for r in requested if r]

    # Next steps and who owns each: an assignee has claimed the PR; otherwise a requested reviewer, or the
    # maintainer the contributor answered; otherwise anyone ("owners" empty).
    claim = [("assigned", l) for l in assignees]
    steps = []

    def step(kind, label, url, owners):
        steps.append({"kind": kind, "label": label, "url": url, "owners": owners})

    if runs:
        step("approve", "Approve the CI runs waiting for a first-time contributor", runs[0]["html_url"], claim)
    if turn == "maintainers":
        if approved and not changes:
            stale = last_push and max(approved.values()) < last_push
            if failing:
                step("merge", "Approved, but checks are failing; look at them before merging", f"{pr['url']}/checks", claim)
            elif not validation_ran:
                step("merge", "Approved; validation has not run, so get it to run, then merge", pr["url"], claim)
            elif stale:
                step("merge", "Approved before the latest push; check the update, then merge", f"{pr['url']}/files", claim)
            else:
                step("merge", "Approved and checks pass: merge", pr["url"], claim)
        elif last_m is None:
            step("first", "First review", pr["url"], claim or [("requested", l) for l in requested])
        else:
            owners = claim or [("requested", l) for l in requested] or [("updated" if source_update else "replied", last_m["who"])]
            label = ("Review the new source commits" if source_update else "The contributor answered; review the update")
            url = src.get("commits_url") if source_update and src.get("commits_url") else pr["url"]
            step("respond", label, url, owners)
        if "status:awaiting-response" in labels:
            step("label", "Remove the stale status:awaiting-response label (the contributor has replied)", pr["url"], [])

    ask = next((e for e in reversed(m_events) if e.get("text")), None)
    reply = next((e for e in reversed(c_events) if e["kind"] != "commit" and e.get("text")), None)

    def ev(e):
        return {"t": iso(e["t"]), "who": e["who"], "role": e["role"], "kind": e["kind"], "url": e["url"],
                "text": e.get("text", ""), "ai": e.get("ai", False)} if e else None

    return {
        "author": author, "turn": turn, "reason": reason, "since": iso(since), "source_update": iso(source_update),
        "first_response": iso(m_events[0]["t"]) if m_events else None,
        "last_maintainer": last_m["who"] if last_m else None,
        "ask": ev(ask), "reply": ev(reply),
        "approved_by": sorted(approved), "changes_by": sorted(changes),
        "assignees": assignees, "review_requests": requested, "labels": labels,
        "checks": checks, "failing": [c["name"] for c in failing], "validation_ran": validation_ran,
        "pending_run": runs[0]["html_url"] if runs else None,
        "steps": [{**s, "owners": [{"why": w, "login": l} for w, l in s["owners"]]} for s in steps],
        "events": [ev(e) for e in events[-12:]],
        "maintainers_seen": sorted({e["who"] for e in m_events}),
    }


# ----------------------------------------------------------------------------- baseline

def historical_baseline(repo, since="2024-01-01"):
    vals, cursor = [], None
    q = f"repo:{repo} is:pr is:merged created:>={since}"
    while True:
        d = graphql(HISTORY_QUERY, q=q, cursor=cursor)["search"]
        for n in d["nodes"]:
            if not n:
                continue
            files = n["files"]["nodes"]
            if n["files"]["totalCount"] != 1 or not is_entry_path(files[0]["path"]) or files[0]["changeType"] != "ADDED":
                continue
            vals.append(days(parse_time(n["createdAt"]), parse_time(n["mergedAt"])))
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]
    vals.sort()
    pick = lambda f: vals[min(len(vals) - 1, int(f * len(vals)))] if vals else 0  # noqa: E731
    return {"n": len(vals), "since": since, "p50": round(statistics.median(vals), 1) if vals else 0,
            "p75": round(pick(.75), 1), "p90": round(pick(.9), 1), "computed_at": iso(now_utc())}


# ----------------------------------------------------------------------------- main

def load_previous(where):
    if not where:
        return {}
    try:
        if re.match(r"https?://", where):
            return request(where, auth=False, timeout=30) or {}
        with open(where) as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 - a missing or broken previous snapshot just means no cache
        print(f"no previous snapshot ({e})", file=sys.stderr)
        return {}


def load_maintainers(path):
    try:
        with open(path) as f:
            return {line.split("#", 1)[0].strip().lower() for line in f} - {""}
    except OSError:
        return set()


def strip_volatile(o):
    if isinstance(o, dict):
        return {k: strip_volatile(v) for k, v in o.items() if k not in ("checked_at", "computed_at")}
    if isinstance(o, list):
        return [strip_volatile(v) for v in o]
    return o


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="Slicer/ExtensionsIndex")
    ap.add_argument("--out", required=True, help="Directory for data.json and version.json")
    ap.add_argument("--previous", help="Previous data.json (URL or path), reused as a cache")
    ap.add_argument("--default-me", help="GitHub login preselected in the page's 'You are' menu")
    ap.add_argument("--run-url", help="Link to the workflow run that produced this snapshot")
    ap.add_argument("--maintainers", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintainers.txt"),
                    help="File of maintainer logins, one per line")
    ap.add_argument("--full", action="store_true", help="Ignore the cache and re-check every source repository")
    args = ap.parse_args()
    TOKEN = token()
    MAINTAINERS.update(load_maintainers(args.maintainers))
    owner, name = args.repo.split("/")
    now = now_utc()
    prev = {} if args.full else load_previous(args.previous)
    if prev.get("schema") != SCHEMA or prev.get("repo") != args.repo:
        prev = {}
    prev_prs = {p["number"]: p for p in prev.get("prs", [])}

    prs, cursor = [], None
    while True:
        d = graphql(PR_QUERY, owner=owner, name=name, cursor=cursor)["repository"]["pullRequests"]
        prs += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]

    pending = {}
    for r in (rest(f"repos/{args.repo}/actions/runs", status="action_required", per_page=100) or {}).get("workflow_runs", []):
        pending.setdefault(r["head_sha"], []).append(r)

    out_prs, infra = [], []
    for pr in prs:
        paths = [f["path"] for f in pr["files"]["nodes"]]
        entry_files = [p for p in paths if is_entry_path(p)]
        if not entry_files or len(entry_files) != len(paths):
            infra.append({"number": pr["number"], "title": pr["title"], "url": pr["url"],
                          "author": pr["author"]["login"] if pr["author"] else "ghost", "created": pr["createdAt"]})
            continue
        old = prev_prs.get(pr["number"], {})
        if old.get("head") == pr["headRefOid"] and "entry" in old:
            entry, entry_text = old["entry"], old.get("entry_text", "")
        else:
            entry, entry_text = {}, ""
            for f in rest(f"repos/{args.repo}/pulls/{pr['number']}/files", per_page=50) or []:
                if f["filename"] in entry_files and f.get("status") != "removed":
                    entry, entry_text = entry_from_patch(f.get("patch"))
                    break
        src_old = old.get("src") or {}
        same_target = (src_old.get("url") == re.sub(r"\.git$", "", (entry.get("scm_url") or "").strip())
                       and src_old.get("rev") == (entry.get("scm_revision") or src_old.get("default_branch")))
        fresh = src_old.get("checked_at") and now - parse_time(src_old["checked_at"]) < SOURCE_TTL
        src = src_old if (same_target and fresh and not src_old.get("error")) else source_info(entry.get("scm_url"), entry.get("scm_revision"))
        if src.get("host") == "github" and src.get("rev"):
            src["commits_url"] = f"{src['html']}/commits/{urllib.parse.quote(src['rev'], safe='')}"
        old_scan = old.get("scan") or {}
        scan = old_scan if (old_scan.get("sha") == src.get("head_sha") and old_scan.get("version") == SCAN_VERSION) else None
        if scan is None and src.get("host") == "github" and src.get("head_sha"):
            try:
                scan = scan_source(src)
            except Exception as e:  # noqa: BLE001 - the dashboard must still build if one scan fails
                scan = {"sha": src["head_sha"], "hints": [{"level": "warn", "text": f"Source scan failed: {e}"}]}
        a = analyze(pr, pending, src)
        out_prs.append({
            "number": pr["number"], "title": pr["title"], "url": pr["url"], "created": pr["createdAt"],
            "updated": pr["updatedAt"], "draft": pr["isDraft"], "head": pr["headRefOid"],
            "name": os.path.splitext(entry_files[0])[0], "entry_files": entry_files,
            "entry": entry, "entry_text": entry_text[:4000], "src": src, "scan": scan,
            "hints": entry_checks(entry, src, scan) + ((scan or {}).get("hints") or []),
            **a,
        })
        print(f"  #{pr['number']} {out_prs[-1]['name']}: {a['turn']}", file=sys.stderr)

    baseline = prev.get("baseline")
    if not baseline or not baseline.get("computed_at") or now - parse_time(baseline["computed_at"]) > BASELINE_TTL:
        baseline = historical_baseline(args.repo)

    seen = {m for p in out_prs for m in p["maintainers_seen"]}
    maintainers = KNOWN_MAINTAINERS + sorted(seen - set(KNOWN_MAINTAINERS))
    data = {"schema": SCHEMA, "repo": args.repo, "generated_at": iso(now), "run_url": args.run_url,
            "default_me": args.default_me, "maintainers": maintainers, "baseline": baseline,
            "prs": out_prs, "infra": infra}
    digest = hashlib.sha256(json.dumps(strip_volatile({"prs": out_prs, "infra": infra}), sort_keys=True).encode()).hexdigest()[:16]
    if prev.get("data_hash") == digest and prev.get("data_changed_at"):
        changed_at = prev["data_changed_at"]
    else:
        changed_at = iso(now)
    data.update(data_hash=digest, data_changed_at=changed_at)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "data.json"), "w") as f:
        json.dump(data, f, separators=(",", ":"))
    with open(os.path.join(args.out, "version.json"), "w") as f:
        json.dump({"data_hash": digest, "generated_at": data["generated_at"], "data_changed_at": changed_at}, f)
    print(f"{len(out_prs)} extension PRs, {len(infra)} other; API calls: {STATS}", file=sys.stderr)


if __name__ == "__main__":
    main()
