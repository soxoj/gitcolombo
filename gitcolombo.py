#!/usr/bin/env python3
"""Gitcolombo — OSINT tool: extract account info from git repositories.

Walks one or more git repositories and aggregates per-person stats
(name, email, author/committer counts, alternate identities) and detects
identity overlaps via shared emails or shared names. Optionally resolves
GitHub logins by scraping commit pages.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable


DELIMITER = "-" * 15

# git log --pretty format: hash;"author_name author_email";"committer_name committer_email"
GIT_LOG_FORMAT = r'%H;"%an %ae";"%cn %ce"'
GIT_LOG_LINE_RE = re.compile(r'(\w+);"(.*?)";"(.*?)"')
GIT_NAME_EMAIL_RE = re.compile(r"^(.*?)\s+(\S+)$")
GITHUB_COMMIT_AUTHOR_RE = re.compile(r'<a href=".+?commits\?author=(.+?)"')

GITHUB_USER_URL = "https://api.github.com/users/{nickname}"
GITHUB_REPOS_URL = (
    "https://api.github.com/users/{nickname}/repos?per_page={per_page}&page={page}"
)
GITHUB_PER_PAGE = 100

HTTP_TIMEOUT = 15
HTTP_USER_AGENT = "gitcolombo/0.2"
RESOLVE_WORKERS = 8
CLONE_WORKERS = 8
DEFAULT_REPOS_DIR = "repos"

GITHUB_GPG_KEYS_URL = "https://api.github.com/users/{nickname}/gpg_keys"
GITHUB_SEARCH_COMMITS_URL = (
    "https://api.github.com/search/commits?q=author:{nickname}"
    "&per_page={per_page}&page={page}"
)
GITHUB_SEARCH_MAX_PAGES = 10  # /search/* caps results at 1000

# Well-known git trailer keys (DCO sign-off, GitHub co-authorship, kernel reviews).
# A real email in any of these is a strong identity signal: trailers are
# typically added intentionally by tooling (`git commit -s`, GitHub UI's
# "Co-authored-by", patch-review workflows) rather than being auto-generated.
TRAILER_RE = re.compile(
    r"^(?P<key>Signed-off-by|Co-authored-by|Reviewed-by|Tested-by|"
    r"Reported-by|Acked-by|Suggested-by|Cc):\s+"
    r"(?P<name>[^<]+?)\s+<(?P<email>[^>]+)>\s*$",
    re.MULTILINE | re.IGNORECASE,
)

SYSTEM_EMAILS = frozenset({"noreply@github.com"})

logger = logging.getLogger("gitcolombo")

# Service noreply addresses from any vendor (github, anthropic, gitlab, ...)
# plus GitHub's user-private `{id}+{login}@users.noreply.github.com` pattern.
SYSTEM_EMAIL_RE = re.compile(
    r'(^(?:noreply|no-reply|donotreply|do-not-reply)@|@users\.noreply\.github\.com$)',
    re.IGNORECASE,
)


def is_system_email(email):
    return bool(email and SYSTEM_EMAIL_RE.search(email))


# ---------- Terminal styling ----------

# ANSI 256-color palette, picked to mirror the web UI's green-on-black look.
NEON = "\033[38;5;46m"   # primary bright green
LIME = "\033[38;5;82m"   # highlight (slightly lighter)
GREEN_DIM = "\033[38;5;34m"   # secondary green
GREY = "\033[38;5;240m"  # faint borders / dot-leaders
RED = "\033[38;5;196m"   # warnings / noreply tags
BOLD = "\033[1m"
RESET = "\033[0m"

BANNER = r"""
 ░██████╗░██╗████████╗░█████╗░░█████╗░██╗░░░░░░█████╗░███╗░░░███╗██████╗░░█████╗░
 ██╔════╝░██║╚══██╔══╝██╔══██╗██╔══██╗██║░░░░░██╔══██╗████╗░████║██╔══██╗██╔══██╗
 ██║░░██╗░██║░░░██║░░░██║░░╚═╝██║░░██║██║░░░░░██║░░██║██╔████╔██║██████╦╝██║░░██║
 ██║░░╚██╗██║░░░██║░░░██║░░██╗██║░░██║██║░░░░░██║░░██║██║╚██╔╝██║██╔══██╗██║░░██║
 ╚██████╔╝██║░░░██║░░░╚█████╔╝╚█████╔╝███████╗╚█████╔╝██║░╚═╝░██║██████╦╝╚█████╔╝
 ░╚═════╝░╚═╝░░░╚═╝░░░░╚════╝░░╚════╝░╚══════╝░╚════╝░╚═╝░░░░░╚═╝╚═════╝░░╚════╝░
                         :: git commit osint ::
"""

_COLOR_ENABLED = False
RULE_WIDTH = 80


def _setup_colors(force_off: bool) -> None:
    global _COLOR_ENABLED
    if force_off or os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return
    try:
        _COLOR_ENABLED = sys.stdout.isatty()
    except Exception:
        _COLOR_ENABLED = False


def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if _COLOR_ENABLED else text


def _rule(width: int = RULE_WIDTH) -> str:
    return _c(GREY, "─" * width)


def _tag(text: str, color: str = GREEN_DIM) -> str:
    return _c(color, f"[{text}]")


def _email_with_tag(email: str) -> str:
    """Bare email, with a trailing [noreply] tag if it's a service address."""
    out = _c(NEON, email)
    if is_system_email(email):
        out += " " + _tag("noreply", RED)
    return out


def _email_brackets(email: str) -> str:
    """<email> [noreply]? — tag stays outside the angle brackets."""
    out = _c(GREEN_DIM, "<") + _c(NEON, email) + _c(GREEN_DIM, ">")
    if is_system_email(email):
        out += " " + _tag("noreply", RED)
    return out


def _section(title: str) -> list[str]:
    return ["", _rule(), _c(GREEN_DIM, f"[ {title} ]"), _rule(), ""]


# ---------- HTTP helpers ----------

def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None


def _http_get_json(url: str):
    payload = _http_get(url)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Bad JSON from %s: %s", url, exc)
        return None


# ---------- GitHub API ----------

def _gh_authed(url: str, token: str | None):
    """Like _http_get_json but with optional bearer token (for higher rate limits)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": HTTP_USER_AGENT,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Bad JSON from %s: %s", url, exc)
        return None


def get_public_repos_count(nickname: str) -> int:
    data = _http_get_json(GITHUB_USER_URL.format(nickname=nickname))
    if not data:
        return 0
    return int(data.get("public_repos", 0))


def get_github_repos(
    nickname: str, repos_count: int, include_forks: bool = False,
) -> set[str]:
    """Return URLs of *nickname*'s repos. Forks dropped unless include_forks.

    Logs a per-call summary (seen / forks-skipped / failed-pages) at INFO so
    the caller can explain a "245 found → only 31 cloned" gap.
    """
    if repos_count <= 0:
        return set()
    last_page = (repos_count + GITHUB_PER_PAGE - 1) // GITHUB_PER_PAGE
    repos: set[str] = set()
    seen = 0
    forks_skipped = 0
    failed_pages = 0
    for page in range(1, last_page + 1):
        data = _http_get_json(
            GITHUB_REPOS_URL.format(
                nickname=nickname, per_page=GITHUB_PER_PAGE, page=page,
            )
        )
        if not data:
            failed_pages += 1
            logger.warning(
                "repos listing page %d/%d returned no data (rate limit? "
                "try GITHUB_TOKEN env var)", page, last_page,
            )
            continue
        for repo in data:
            seen += 1
            if repo.get("fork") and not include_forks:
                forks_skipped += 1
                continue
            repos.add(repo["html_url"])
    logger.info(
        "listing: %d seen, %d forks %s, %d kept%s",
        seen,
        forks_skipped,
        "kept" if include_forks else "skipped",
        len(repos),
        f", {failed_pages} page(s) failed" if failed_pages else "",
    )
    return repos


def resolve_github_username(repo_url: str, commit_hash: str) -> str | None:
    """Scrape commit page to find the GitHub login behind an email."""
    if not repo_url.startswith("https://github.com/"):
        return None
    commit_url = f"{repo_url.rstrip('/')}/commit/{commit_hash}"
    page = _http_get(commit_url)
    if page is None:
        return None
    match = GITHUB_COMMIT_AUTHOR_RE.search(page.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def get_gpg_keys_emails(nickname: str, token: str | None = None):
    """Fetch user-uploaded PGP keys via /users/{u}/gpg_keys and yield emails.

    These emails come from the key's UIDs — the user uploaded them themselves,
    so this is a direct identity disclosure. `verified=True` means GitHub has
    confirmed the user controls that mailbox.

    Yields dicts: {email, verified, key_id, created_at, source}.
    """
    keys = _gh_authed(GITHUB_GPG_KEYS_URL.format(nickname=nickname), token)
    if not keys:
        return
    seen: set[str] = set()

    def _walk(key, source):
        if not key or key.get("revoked"):
            return
        key_id = key.get("key_id", "")
        created = key.get("created_at", "")
        for entry in (key.get("emails") or []):
            email = entry.get("email")
            if not email:
                continue
            k = email.lower()
            if k in seen:
                continue
            seen.add(k)
            yield {
                "email": email,
                "verified": bool(entry.get("verified")),
                "key_id": key_id,
                "created_at": created,
                "source": source,
            }

    for key in keys:
        yield from _walk(key, "primary")
        for sub in (key.get("subkeys") or []):
            yield from _walk(sub, "subkey")


def print_gpg_results(results, ignore_noreply: bool = True) -> bool:
    """Pretty-print get_gpg_keys_emails() output. Returns True if printed."""
    rows = [
        r for r in results
        if not (ignore_noreply and is_system_email(r["email"]))
    ]
    if not rows:
        return False
    for line in _section("pgp key uids"):
        print(line)
    print("  " + _c(GREEN_DIM, "source: /users/{u}/gpg_keys (user-uploaded)"))
    print()
    rows.sort(key=lambda r: (not r["verified"], r["email"]))
    for r in rows:
        flag_color = LIME if r["verified"] else GREEN_DIM
        flag = _tag("verified" if r["verified"] else "unverified", flag_color)
        print("  {arrow} {email:40} {flag}  {kid}={key}  {src}".format(
            arrow=_c(LIME, "▶"),
            email=_email_with_tag(r["email"]),
            flag=flag,
            kid=_c(GREEN_DIM, "key_id"),
            key=_c(NEON, r["key_id"] or "?"),
            src=_tag(r["source"]),
        ))
    print()
    return True


def search_commits_by_author(nickname: str, token: str | None = None):
    """Use /search/commits?q=author:{u} to find commits across all of public GitHub.

    Also extracts well-known git trailers from each commit message body
    (Signed-off-by, Co-authored-by, Reviewed-by, etc.).

    Yields dicts: {email, name, role, repo, sha, date}.
    """
    seen: set[tuple[str, str, str]] = set()
    for page in range(1, GITHUB_SEARCH_MAX_PAGES + 1):
        url = GITHUB_SEARCH_COMMITS_URL.format(
            nickname=nickname, per_page=GITHUB_PER_PAGE, page=page,
        )
        data = _gh_authed(url, token)
        if not data:
            return
        items = data.get("items") or []
        if not items:
            return
        for item in items:
            commit = item.get("commit") or {}
            repo = (item.get("repository") or {}).get("full_name", "")
            sha = item.get("sha", "")
            date = (commit.get("author") or {}).get("date", "")
            message = commit.get("message") or ""
            for role in ("author", "committer"):
                who = commit.get(role) or {}
                email = who.get("email")
                name = who.get("name") or ""
                if not email:
                    continue
                key = (email.lower(), name.lower(), role)
                if key in seen:
                    continue
                seen.add(key)
                yield {"email": email, "name": name, "role": role,
                       "repo": repo, "sha": sha, "date": date}
            # trailers in the commit message body
            for tm in TRAILER_RE.finditer(message):
                t_key = tm.group("key").lower()
                t_name = (tm.group("name") or "").strip()
                t_email = (tm.group("email") or "").strip()
                if not t_email:
                    continue
                # reject malformed names: ':' implies another trailer label was
                # crammed onto the same line; '@' implies a @-mention or stray
                # handle. Real personal names don't contain either.
                if ":" in t_name or "@" in t_name:
                    continue
                key = (t_email.lower(), t_name.lower(), t_key)
                if key in seen:
                    continue
                seen.add(key)
                yield {"email": t_email, "name": t_name, "role": t_key,
                       "repo": repo, "sha": sha, "date": date}
        if len(items) < GITHUB_PER_PAGE:
            return


def print_search_results(results, ignore_noreply: bool = True) -> None:
    """Pretty-print search_commits_by_author() output grouped by (email, name)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        if ignore_noreply and is_system_email(r["email"]):
            continue
        key = (r["email"], r["name"])
        groups.setdefault(key, []).append(r)

    if not groups:
        print(_c(RED, "[!] no public commits found via /search/commits"))
        return

    for line in _section("commit search"):
        print(line)
    print("  " + _c(GREEN_DIM, "identities found: ") + _c(NEON, str(len(groups))))
    print()
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for (email, name), rows in ordered:
        repos = sorted({r["repo"] for r in rows if r["repo"]})
        roles = sorted({r["role"] for r in rows})
        print("  {arrow} {name} {brackets}  {hits}  {roles}".format(
            arrow=_c(LIME, "▶"),
            name=_c(BOLD + NEON, name or "?"),
            brackets=_email_brackets(email),
            hits=_c(LIME, f"×{len(rows)}"),
            roles=_tag(", ".join(roles)),
        ))
        for i, repo in enumerate(repos[:5]):
            last = i == min(4, len(repos) - 1) and len(repos) <= 5
            branch = "└─" if last else "├─"
            print("      " + _c(GREEN_DIM, branch) + " "
                  + _c(GREEN_DIM, "repo  ") + _c(NEON, repo))
        if len(repos) > 5:
            print("      " + _c(GREEN_DIM, "└─ ")
                  + _c(GREEN_DIM, f"... +{len(repos) - 5} more repos"))
        print()


# ---------- Filesystem helpers ----------

def find_all_repos_recursively(path: str) -> list[str]:
    """Return repo roots (directories that contain a .git subdir) under path."""
    repos: list[str] = []
    for current_dir, dirs, _ in os.walk(path):
        if ".git" in dirs:
            repos.append(current_dir)
            dirs[:] = [d for d in dirs if d != ".git"]
    return repos


# ---------- Git subprocess ----------

def git_log(repo_dir: str) -> str:
    try:
        result = subprocess.run(
            ["git", "log", f"--pretty={GIT_LOG_FORMAT}", "--all"],
            cwd=repo_dir, check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        logger.error("'git' binary not found")
        return ""
    if result.returncode != 0:
        logger.debug("git log failed in %s: %s", repo_dir, result.stderr.strip())
    return result.stdout


def _clone_target_dir(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def git_clone(url: str, dest_dir: str) -> str | None:
    """Clone *url* into *dest_dir*/<repo-name>. Returns the cloned path or None."""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, _clone_target_dir(url))
    try:
        result = subprocess.run(
            ["git", "clone", url, target],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        logger.error("'git' binary not found")
        return None
    if result.returncode != 0:
        logger.debug("git clone failed for %s: %s", url, result.stderr.strip())
        return None
    return target


def _short_url(url: str, width: int = 50) -> str:
    """Trim URL for progress display: keep owner/repo tail."""
    if len(url) <= width:
        return url
    tail = "/".join(url.rstrip("/").split("/")[-2:])
    return ("…" + tail)[-width:]


def clone_many(
    urls: list[str],
    dest_dir: str,
    workers: int = CLONE_WORKERS,
) -> dict[str, str | None]:
    """Clone *urls* concurrently. Returns {url: local_path or None}.

    Prints a live progress line to stderr (overwritten on TTY, line-per-tick
    otherwise) so the user can see what's happening during long clone batches.
    """
    total = len(urls)
    if total == 0:
        return {}

    results: dict[str, str | None] = {}
    state = {"done": 0, "ok": 0, "fail": 0, "current": ""}
    lock = threading.Lock()
    started = time.monotonic()
    is_tty = False
    try:
        is_tty = sys.stderr.isatty()
    except Exception:
        pass

    last_done = {"value": -1}

    def render(final: bool = False) -> None:
        elapsed = time.monotonic() - started
        fail_chunk = _c(RED, f"fail={state['fail']}") if state["fail"] else \
                     _c(GREEN_DIM, "fail=0")
        line = (
            _c(GREEN_DIM, "[*] ")
            + _c(LIME, "cloning ")
            + _c(NEON, f"{state['done']}/{total}")
            + "  " + _c(GREEN_DIM, f"ok={state['ok']}")
            + "  " + fail_chunk
            + "  " + _c(GREEN_DIM, f"{elapsed:>4.0f}s")
        )
        if state["current"] and not final:
            line += "  " + _c(GREEN_DIM, "· ") + _c(NEON, state["current"])
        if is_tty:
            # \r + clear-to-end-of-line keeps the progress on a single line.
            sys.stderr.write("\r\033[K" + line)
            if final:
                sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            # Non-TTY: avoid a flood of identical "0/N" lines while threads
            # pick up their first job. Only emit when the done counter ticks
            # forward (or on the final summary).
            if final or state["done"] != last_done["value"]:
                last_done["value"] = state["done"]
                sys.stderr.write(line + "\n")

    def worker(url: str) -> None:
        with lock:
            state["current"] = _short_url(url)
            render()
        path = git_clone(url, dest_dir)
        with lock:
            state["done"] += 1
            if path:
                state["ok"] += 1
            else:
                state["fail"] += 1
            # Don't keep stale "current" once this thread is done; the next
            # worker that picks up a job will overwrite it.
            state["current"] = ""
            render()

    render()  # initial 0/total
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(worker, url): url for url in urls}
            for fut in futures:
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("clone worker for %s raised: %s", futures[fut], exc)
    finally:
        with lock:
            state["current"] = ""
            render(final=True)

    # Map each URL to its deterministic target path so callers get a stable
    # {url: path|None} contract regardless of completion order.
    for url in urls:
        target = os.path.join(dest_dir, _clone_target_dir(url))
        results[url] = target if os.path.isdir(os.path.join(target, ".git")) else None
    return results


# ---------- Data classes ----------

def _split_name_email(raw: str) -> tuple[str, str]:
    m = GIT_NAME_EMAIL_RE.match(raw)
    if not m:
        logger.error("Could not extract name/email from %r", raw)
        return "", ""
    return m.group(1), m.group(2)


@dataclass
class Commit:
    hash: str
    author: str
    committer: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str

    @property
    def author_committer_same(self) -> bool:
        return (
            self.author_name == self.committer_name
            and self.author_email == self.committer_email
        )

    @classmethod
    def parse(cls, line: str) -> "Commit | None":
        m = GIT_LOG_LINE_RE.search(line)
        if not m:
            logger.error("Could not parse commit line %r", line)
            return None
        h, author, committer = m.groups()
        a_name, a_email = _split_name_email(author)
        c_name, c_email = _split_name_email(committer)
        return cls(h, author, committer, a_name, a_email, c_name, c_email)

    def __str__(self) -> str:
        return (
            f"Hash: {self.hash}\n"
            f"Author name: {self.author_name}\n"
            f"Author email: {self.author_email}\n"
            f"Committer name: {self.committer_name}\n"
            f"Committer email: {self.committer_email}\n"
        )


@dataclass
class Person:
    key: str
    name: str = ""
    email: str = ""
    as_author: int = 0
    as_committer: int = 0
    also_known: dict[str, "Person"] = field(default_factory=dict)
    github_login: str | None = None
    repo_url: str | None = None
    last_commit_hash: str | None = None

    def __str__(self) -> str:
        # Headline: ▶ name <email> [noreply]?
        header = "  {arrow} {name} {brackets}".format(
            arrow=_c(LIME, "▶"),
            name=_c(BOLD + NEON, self.name or "?"),
            brackets=_email_brackets(self.email),
        )
        rows: list[tuple[str, str]] = []
        if self.as_author:
            rows.append(("author", _c(LIME, f"×{self.as_author}")))
        if self.as_committer:
            rows.append(("committer", _c(LIME, f"×{self.as_committer}")))
        if self.github_login:
            url = f"https://github.com/{self.github_login}"
            rows.append(("github", _c(LIME, url) + " " + _tag("verified", LIME)))
        for alias in self.also_known.values():
            alias_text = f"{alias.name} {_email_brackets(alias.email)}"
            rows.append(("alias", alias_text))

        lines = [header]
        for i, (label, value) in enumerate(rows):
            branch = "└─" if i == len(rows) - 1 else "├─"
            lines.append(
                "      " + _c(GREEN_DIM, branch) + " "
                + _c(GREEN_DIM, f"{label:<10}") + " " + value
            )
        return "\n".join(lines)


# ---------- Analyst ----------

class GitAnalyst:
    def __init__(self, repos_dir: str = DEFAULT_REPOS_DIR) -> None:
        self.repos_dir = repos_dir
        self.commits: list[Commit] = []
        self.persons: dict[str, Person] = {}
        self.name_to_emails: dict[str, set[str]] = defaultdict(set)
        self.repos: list[str] = []
        self.same_emails_persons: dict[str, tuple[list[str], set[str]]] = {}

    def append(self, source: str, *, cloned_path: str | None = None) -> None:
        if cloned_path is not None:
            repo_dir = cloned_path
        elif "://" in source:
            repo_dir = git_clone(source, self.repos_dir)
            if repo_dir is None:
                return
        else:
            repo_dir = source

        self.repos.append(repo_dir)
        log_output = git_log(repo_dir)
        new_commits = [
            c for c in (Commit.parse(line) for line in log_output.splitlines() if line)
            if c is not None
        ]
        self.commits.extend(new_commits)
        self._analyze(new_commits, source)

    @property
    def sorted_persons(self) -> list[tuple[str, Person]]:
        return sorted(
            self.persons.items(),
            key=lambda item: item[1].as_author + item[1].as_committer,
        )

    def resolve_persons(self) -> None:
        targets = [
            p for p in self.persons.values()
            if p.email not in SYSTEM_EMAILS and p.repo_url and p.last_commit_hash
        ]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
            futures = {
                pool.submit(resolve_github_username, p.repo_url, p.last_commit_hash): p
                for p in targets
            }
            for fut, person in futures.items():
                login = fut.result()
                if login:
                    person.github_login = login

    def _upsert(
        self, key: str, name: str, email: str, repo_url: str, commit_hash: str,
    ) -> Person:
        person = self.persons.get(key) or Person(key=key)
        person.name = name
        person.email = email
        person.repo_url = repo_url
        person.last_commit_hash = commit_hash
        self.persons[key] = person
        return person

    def _analyze(self, new_commits: Iterable[Commit], repo_url: str) -> None:
        for commit in new_commits:
            author = self._upsert(
                commit.author, commit.author_name, commit.author_email,
                repo_url, commit.hash,
            )
            author.as_author += 1

            committer = self._upsert(
                commit.committer, commit.committer_name, commit.committer_email,
                repo_url, commit.hash,
            )
            committer.as_committer += 1

            if not commit.author_committer_same:
                author.also_known[commit.committer] = committer
                committer.also_known[commit.author] = author

            self.name_to_emails[commit.author_name].add(commit.author_email)
            self.name_to_emails[commit.committer_name].add(commit.committer_email)

        # Group names that share the exact same set of emails — these are
        # treated as the same person. O(n) instead of the previous O(n²).
        emails_to_names: dict[frozenset[str], list[str]] = defaultdict(list)
        for name, emails in self.name_to_emails.items():
            emails_to_names[frozenset(emails)].append(name)
        self.same_emails_persons = {
            ",".join(sorted(names)): (sorted(names), set(emails))
            for emails, names in emails_to_names.items()
            if len(names) > 1
        }

    def __str__(self) -> str:
        parts: list[str] = []

        # 1. Stats — top-level summary of what was scanned and what was found.
        parts.extend(_section("stats"))
        for label, value in (
            ("repos",   len(self.repos)),
            ("commits", len(self.commits)),
            ("persons", len(self.persons)),
        ):
            dots = "." * (16 - len(label))
            parts.append("  " + _c(GREEN_DIM, label) + " "
                         + _c(GREY, dots) + " " + _c(NEON, str(value)))
        parts.append("")
        parts.append("  " + _c(GREEN_DIM, "targets"))
        for i, repo in enumerate(self.repos):
            branch = "└─" if i == len(self.repos) - 1 else "├─"
            parts.append("      " + _c(GREEN_DIM, branch) + " " + _c(NEON, repo))

        # 2. Correlation — shared names with multiple emails + same-person clusters.
        matching: list[str] = []
        for name, emails in self.name_to_emails.items():
            if len(emails) <= 1:
                continue
            sorted_emails = sorted(emails)
            block = [
                "  {bang} {name} {arrow} {n} emails".format(
                    bang=_c(RED, "[!]"),
                    name=_c(BOLD + NEON, name),
                    arrow=_c(GREEN_DIM, "→"),
                    n=_c(LIME, str(len(sorted_emails))),
                )
            ]
            for i, e in enumerate(sorted_emails):
                branch = "└─" if i == len(sorted_emails) - 1 else "├─"
                block.append("      " + _c(GREEN_DIM, branch) + " "
                             + _email_with_tag(e))
            matching.append("\n".join(block))

        same_person: list[str] = []
        for names, _emails in self.same_emails_persons.values():
            joined = _c(BOLD + NEON, (" " + _c(GREEN_DIM, "≡") + " ").join(names))
            same_person.append(
                "  " + _c(RED, "[!]") + " " + _c(GREEN_DIM, "same person:") + " "
                + joined
            )

        if matching or same_person:
            parts.extend(_section("correlation"))
            if matching:
                parts.append("\n\n".join(matching))
                parts.append("")
            if same_person:
                parts.extend(same_person)
                parts.append("")

        # 3. Identities — per-person breakdown.
        parts.extend(_section("identities"))
        for _, person in self.sorted_persons:
            parts.append(str(person))
            parts.append("")

        return "\n".join(parts)


# ---------- CLI ----------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract accounts' information from git repo and make some researches.",
    )
    parser.add_argument("-d", "--dir", help="directory with git project(s)")
    parser.add_argument("-u", "--url", help="url of git repo")
    parser.add_argument(
        "--github", action="store_true",
        help="try to extract extended info from GitHub",
    )
    parser.add_argument(
        "--nickname", type=str,
        help="download repos from GitHub by nickname",
    )
    parser.add_argument(
        "--search", type=str, metavar="USERNAME",
        help="API-only path: query /users/{u}/gpg_keys + /search/commits "
             "for emails (no cloning, ~1000 commit results max)",
    )
    parser.add_argument(
        "--no-ignore-noreply", action="store_true",
        help="do not filter service noreply addresses from --search results",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="recursive directory processing",
    )
    parser.add_argument(
        "--repos-dir", default=DEFAULT_REPOS_DIR,
        help=f"directory to clone remote repositories into (default: {DEFAULT_REPOS_DIR})",
    )
    parser.add_argument(
        "--clone-workers", type=int, default=CLONE_WORKERS,
        help=f"parallel git-clone workers (default: {CLONE_WORKERS})",
    )
    parser.add_argument(
        "--include-forks", action="store_true",
        help="include forked repositories (default: skipped — forks add upstream "
             "history that is not the target user's work)",
    )
    parser.add_argument("--debug", action="store_true", help="print debug information")
    parser.add_argument(
        "--no-color", action="store_true",
        help="disable ANSI colors (also honored via NO_COLOR env var or non-TTY stdout)",
    )
    return parser.parse_args()


def _collect_sources(args: argparse.Namespace) -> list[str]:
    sources: list[str] = []
    if args.url:
        sources.append(args.url)
    if args.dir:
        sources.append(args.dir.rstrip("/"))
        if args.recursive:
            sources.extend(find_all_repos_recursively(args.dir))
    if args.nickname:
        count = get_public_repos_count(args.nickname)
        if count:
            logger.info("found %d public repos for %s", count, args.nickname)
            sources.extend(get_github_repos(
                args.nickname, repos_count=count,
                include_forks=args.include_forks,
            ))
    return sources


def main() -> None:
    args = _parse_args()
    _setup_colors(force_off=args.no_color)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format=_c(GREEN_DIM, "[*] ") + _c(LIME, "%(levelname)s") + " %(message)s",
    )

    print(_c(NEON, BANNER), flush=True)

    if args.search:
        token = os.environ.get("GITHUB_TOKEN")
        ignore = not args.no_ignore_noreply
        gpg = list(get_gpg_keys_emails(args.search, token=token))
        had_gpg = print_gpg_results(gpg, ignore_noreply=ignore)
        results = list(search_commits_by_author(args.search, token=token))
        print_search_results(results, ignore_noreply=ignore)
        if not had_gpg and not results:
            print("No emails found via /gpg_keys or /search/commits.")
        return

    sources = _collect_sources(args)
    if not sources:
        print("Run me with git repo link or path!")
        return

    analyst = GitAnalyst(repos_dir=args.repos_dir)

    url_sources = [s for s in sources if "://" in s]
    local_sources = [s for s in sources if "://" not in s]

    cloned: dict[str, str | None] = {}
    if url_sources:
        logger.info(
            "cloning %d repo(s) into %s with %d workers",
            len(url_sources), args.repos_dir, args.clone_workers,
        )
        cloned = clone_many(url_sources, args.repos_dir, workers=args.clone_workers)
        failed = [u for u, p in cloned.items() if p is None]
        if failed:
            logger.warning("%d clone(s) failed (see --debug for reasons)", len(failed))

    to_analyze = len(local_sources) + sum(1 for p in cloned.values() if p)
    if to_analyze:
        logger.info("analyzing %d repo(s)...", to_analyze)
    for src in local_sources:
        analyst.append(src)
    for url, path in cloned.items():
        if path:
            analyst.append(url, cloned_path=path)

    if analyst.persons:
        logger.info("resolving GitHub usernames for %d identities...",
                    len(analyst.persons))
    analyst.resolve_persons()

    if analyst.repos:
        print(analyst)
    else:
        print("Run me with git repo link or path!")


if __name__ == "__main__":
    main()
