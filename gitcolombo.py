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
DEFAULT_REPOS_DIR = "repos"

SYSTEM_EMAILS = frozenset({"noreply@github.com"})

logger = logging.getLogger("gitcolombo")


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

def get_public_repos_count(nickname: str) -> int:
    data = _http_get_json(GITHUB_USER_URL.format(nickname=nickname))
    if not data:
        return 0
    return int(data.get("public_repos", 0))


def get_github_repos(
    nickname: str, repos_count: int, include_forks: bool = False,
) -> set[str]:
    if repos_count <= 0:
        return set()
    last_page = (repos_count + GITHUB_PER_PAGE - 1) // GITHUB_PER_PAGE
    repos: set[str] = set()
    for page in range(1, last_page + 1):
        data = _http_get_json(
            GITHUB_REPOS_URL.format(
                nickname=nickname, per_page=GITHUB_PER_PAGE, page=page,
            )
        )
        if not data:
            continue
        for repo in data:
            if include_forks or not repo.get("fork"):
                repos.add(repo["html_url"])
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
        lines = [
            f"Name:\t\t\t{self.name}",
            f"Email:\t\t\t{self.email}",
        ]
        if self.as_author:
            lines.append(f"Appears as author:\t{self.as_author} times")
        if self.as_committer:
            lines.append(f"Appears as committer:\t{self.as_committer} times")
        if self.github_login:
            lines.append(
                f"Verified account:\n\t\t\thttps://github.com/{self.github_login}"
            )
        if self.also_known:
            lines.append(
                "Also appears with:" + "".join(f"\n\t\t\t{k}" for k in self.also_known)
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

    def append(self, source: str) -> None:
        if "://" in source:
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
        parts: list[str] = [
            f'Analyze of the git repo(s) "{", ".join(self.repos)}"',
            "",
            "Verbose persons info:",
        ]
        for _, person in self.sorted_persons:
            parts.append(DELIMITER)
            parts.append(str(person))

        matching: list[str] = []
        for name, emails in self.name_to_emails.items():
            if len(emails) > 1:
                emails_block = "\n\t\t\t".join(sorted(emails))
                matching.append(
                    f"\n{name} is the owner of emails:\n\t\t\t{emails_block}"
                )
        if matching:
            parts.append("")
            parts.append("Matching info:")
            parts.append(DELIMITER + "".join(matching))

        for names, _ in self.same_emails_persons.values():
            parts.append(f"\n{' and '.join(names)} are the same person")

        parts.append("")
        parts.append("Statistics info:")
        parts.append(DELIMITER)
        parts.append(f"Total persons: {len(self.persons)}")
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
        "-r", "--recursive", action="store_true",
        help="recursive directory processing",
    )
    parser.add_argument(
        "--repos-dir", default=DEFAULT_REPOS_DIR,
        help=f"directory to clone remote repositories into (default: {DEFAULT_REPOS_DIR})",
    )
    parser.add_argument("--debug", action="store_true", help="print debug information")
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
            print(f"found {count} repos")
            sources.extend(get_github_repos(args.nickname, repos_count=count))
    return sources


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="-" * 40 + "\n%(levelname)s: %(message)s",
    )

    sources = _collect_sources(args)
    if not sources:
        print("Run me with git repo link or path!")
        return

    analyst = GitAnalyst(repos_dir=args.repos_dir)
    for source in sources:
        analyst.append(source)

    logger.info("Resolving GitHub usernames, please wait...")
    analyst.resolve_persons()

    if analyst.repos:
        print(analyst)
    else:
        print("Run me with git repo link or path!")


if __name__ == "__main__":
    main()
