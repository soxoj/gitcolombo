"""Tests for gitcolombo.

Each test class is wired to one of the bugs / regressions documented in the
refactor: invalid commit lines, missing Person attributes, unsafe subprocess
calls, .git-suffix handling, recursive repo lookup, HTTP timeouts, O(n^2)
identity matching, None sources, system-email skipping, and pagination math.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from io import BytesIO
from unittest import mock

import gitcolombo as gc


# ---------- Commit parsing ----------

class TestCommitParse(unittest.TestCase):
    def test_valid_line(self):
        line = 'abc123;"Alice alice@example.com";"Bob bob@example.com"'
        c = gc.Commit.parse(line)
        self.assertIsNotNone(c)
        self.assertEqual(c.hash, "abc123")
        self.assertEqual(c.author_name, "Alice")
        self.assertEqual(c.author_email, "alice@example.com")
        self.assertEqual(c.committer_name, "Bob")
        self.assertEqual(c.committer_email, "bob@example.com")

    def test_invalid_line_returns_none(self):
        # Regression: original Commit.__init__ left attributes unset on bad
        # input and crashed downstream with AttributeError.
        self.assertIsNone(gc.Commit.parse("garbage"))
        self.assertIsNone(gc.Commit.parse(""))

    def test_author_committer_same(self):
        line = 'h;"Alice alice@x.io";"Alice alice@x.io"'
        c = gc.Commit.parse(line)
        self.assertTrue(c.author_committer_same)

    def test_author_committer_different(self):
        line = 'h;"Alice alice@x.io";"Alice bob@x.io"'
        c = gc.Commit.parse(line)
        self.assertFalse(c.author_committer_same)

    def test_split_name_email_invalid(self):
        # Garbage in must not crash, just return empty strings.
        self.assertEqual(gc._split_name_email("no-space-here"), ("", ""))


# ---------- Person dataclass ----------

class TestPerson(unittest.TestCase):
    def test_default_attributes_present(self):
        # Regression: original Person did not initialize repo_url / commit /
        # github_link in __init__, so accessing them raised AttributeError.
        p = gc.Person(key="Alice alice@x.io")
        self.assertIsNone(p.repo_url)
        self.assertIsNone(p.last_commit_hash)
        self.assertIsNone(p.github_login)
        self.assertEqual(p.also_known, {})
        self.assertEqual(p.as_author, 0)
        self.assertEqual(p.as_committer, 0)

    def test_str_minimal(self):
        p = gc.Person(key="x", name="Alice", email="a@x.io")
        s = str(p)
        self.assertIn("Alice", s)
        self.assertIn("a@x.io", s)


# ---------- Clone target directory normalization ----------

class TestCloneTargetDir(unittest.TestCase):
    def test_plain_url(self):
        self.assertEqual(
            gc._clone_target_dir("https://github.com/user/repo"), "repo",
        )

    def test_dot_git_suffix(self):
        # Regression: original code used source.split('/')[-1] verbatim and
        # ended up looking for a directory named "repo.git" that git clone
        # never created.
        self.assertEqual(
            gc._clone_target_dir("https://github.com/user/repo.git"), "repo",
        )

    def test_trailing_slash(self):
        self.assertEqual(
            gc._clone_target_dir("https://github.com/user/repo/"), "repo",
        )

    def test_dot_git_with_trailing_slash(self):
        self.assertEqual(
            gc._clone_target_dir("https://github.com/user/repo.git/"), "repo",
        )


# ---------- Subprocess safety (no shell=True) ----------

class TestSubprocessSafety(unittest.TestCase):
    def test_git_log_uses_argv_not_shell(self):
        # Regression: original used Popen(cmd, shell=True) — command
        # injection if a malicious path/URL were ever passed.
        with mock.patch("gitcolombo.subprocess.run") as run:
            run.return_value = types.SimpleNamespace(
                returncode=0, stdout="", stderr="",
            )
            gc.git_log("/tmp/repo")
            args, kwargs = run.call_args
            cmd = args[0]
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[0], "git")
            self.assertEqual(cmd[1], "log")
            self.assertNotIn("shell", kwargs)  # default is False

    def test_git_clone_uses_argv_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gitcolombo.subprocess.run") as run:
                run.return_value = types.SimpleNamespace(
                    returncode=0, stdout="", stderr="",
                )
                url = "https://example.com/repo.git"
                gc.git_clone(url, tmp)
                args, kwargs = run.call_args
                self.assertEqual(
                    args[0],
                    ["git", "clone", url, os.path.join(tmp, "repo")],
                )
                self.assertNotIn("shell", kwargs)

    def test_git_log_returns_empty_when_binary_missing(self):
        with mock.patch("gitcolombo.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(gc.git_log("/tmp/whatever"), "")

    def test_git_clone_returns_none_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gitcolombo.subprocess.run") as run:
                run.return_value = types.SimpleNamespace(
                    returncode=128, stdout="", stderr="fatal",
                )
                self.assertIsNone(gc.git_clone("https://x/y", tmp))

    def test_git_clone_creates_dest_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "deep", "nested")
            with mock.patch("gitcolombo.subprocess.run") as run:
                run.return_value = types.SimpleNamespace(
                    returncode=0, stdout="", stderr="",
                )
                gc.git_clone("https://x/repo", dest)
            self.assertTrue(os.path.isdir(dest))


# ---------- Recursive repo discovery ----------

class TestFindAllReposRecursively(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # /tmp/root/a/.git, /tmp/root/b/c/.git, /tmp/root/d (not a repo)
        os.makedirs(os.path.join(self.tmp, "a", ".git"))
        os.makedirs(os.path.join(self.tmp, "b", "c", ".git"))
        os.makedirs(os.path.join(self.tmp, "d"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_repo_roots_not_dot_git(self):
        # Regression: original returned `.git` dirs themselves.
        repos = gc.find_all_repos_recursively(self.tmp)
        self.assertIn(os.path.join(self.tmp, "a"), repos)
        self.assertIn(os.path.join(self.tmp, "b", "c"), repos)
        for r in repos:
            self.assertFalse(r.endswith(".git"))

    def test_does_not_descend_into_dot_git(self):
        # If .git itself contained another .git (it doesn't normally), we
        # shouldn't report it. Create a decoy.
        os.makedirs(os.path.join(self.tmp, "a", ".git", "nested", ".git"))
        repos = gc.find_all_repos_recursively(self.tmp)
        for r in repos:
            self.assertNotIn(os.sep + ".git" + os.sep, r + os.sep)


# ---------- HTTP timeout ----------

class TestHttpTimeout(unittest.TestCase):
    def test_http_get_passes_timeout(self):
        # Regression: original urlopen calls had no timeout — could hang.
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value.read.return_value = b"ok"
        fake_resp.__exit__.return_value = False
        with mock.patch("gitcolombo.urllib.request.urlopen", return_value=fake_resp) as up:
            gc._http_get("https://example.com/x")
            _, kwargs = up.call_args
            self.assertIn("timeout", kwargs)
            self.assertEqual(kwargs["timeout"], gc.HTTP_TIMEOUT)

    def test_http_get_returns_none_on_error(self):
        import urllib.error
        with mock.patch(
            "gitcolombo.urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            self.assertIsNone(gc._http_get("https://example.com/x"))


# ---------- GitHub pagination math ----------

class TestGitHubPagination(unittest.TestCase):
    def test_zero_repos(self):
        # Regression: get_github_repos used to compute pagination even for
        # count=0/None, doing one wasted request.
        with mock.patch("gitcolombo._http_get_json") as get:
            result = gc.get_github_repos("nobody", repos_count=0)
            self.assertEqual(result, set())
            get.assert_not_called()

    def test_exact_page_boundary(self):
        # 100 repos == exactly one page, not two.
        with mock.patch("gitcolombo._http_get_json", return_value=[]) as get:
            gc.get_github_repos("u", repos_count=100)
            self.assertEqual(get.call_count, 1)

    def test_two_pages(self):
        with mock.patch("gitcolombo._http_get_json", return_value=[]) as get:
            gc.get_github_repos("u", repos_count=150)
            self.assertEqual(get.call_count, 2)

    def test_forks_excluded_by_default(self):
        page = [
            {"html_url": "https://github.com/u/r1", "fork": False},
            {"html_url": "https://github.com/u/r2", "fork": True},
        ]
        with mock.patch("gitcolombo._http_get_json", return_value=page):
            result = gc.get_github_repos("u", repos_count=2)
        self.assertEqual(result, {"https://github.com/u/r1"})

    def test_forks_included_when_requested(self):
        page = [
            {"html_url": "https://github.com/u/r1", "fork": False},
            {"html_url": "https://github.com/u/r2", "fork": True},
        ]
        with mock.patch("gitcolombo._http_get_json", return_value=page):
            result = gc.get_github_repos("u", repos_count=2, include_forks=True)
        self.assertEqual(result, {"https://github.com/u/r1", "https://github.com/u/r2"})

    def test_public_repos_count_handles_missing(self):
        with mock.patch("gitcolombo._http_get_json", return_value=None):
            self.assertEqual(gc.get_public_repos_count("ghost"), 0)


# ---------- Identity matching (same_emails_persons) ----------

class TestSameEmailsPersons(unittest.TestCase):
    def _commit(self, h, a_name, a_email, c_name=None, c_email=None):
        c_name = c_name or a_name
        c_email = c_email or a_email
        line = f'{h};"{a_name} {a_email}";"{c_name} {c_email}"'
        return gc.Commit.parse(line)

    def test_two_names_share_email_set(self):
        # Two names with the EXACT same email set must be linked as the
        # same person. This used to be O(n^2) and rebuilt every call.
        analyst = gc.GitAnalyst()
        commits = [
            self._commit("h1", "Alice", "shared@x.io"),
            self._commit("h2", "AliceB", "shared@x.io"),
        ]
        analyst._analyze(commits, "https://example.com/r")
        self.assertEqual(len(analyst.same_emails_persons), 1)
        names, emails = next(iter(analyst.same_emails_persons.values()))
        self.assertEqual(sorted(names), ["Alice", "AliceB"])
        self.assertEqual(emails, {"shared@x.io"})

    def test_disjoint_email_sets_not_linked(self):
        analyst = gc.GitAnalyst()
        commits = [
            self._commit("h1", "Alice", "a@x.io"),
            self._commit("h2", "Bob", "b@x.io"),
        ]
        analyst._analyze(commits, "r")
        self.assertEqual(analyst.same_emails_persons, {})

    def test_one_name_many_emails_reported_in_matching(self):
        analyst = gc.GitAnalyst()
        commits = [
            self._commit("h1", "Alice", "a1@x.io"),
            self._commit("h2", "Alice", "a2@x.io"),
        ]
        analyst._analyze(commits, "r")
        self.assertEqual(analyst.name_to_emails["Alice"], {"a1@x.io", "a2@x.io"})

    def test_author_committer_link(self):
        # author != committer → must show up in each other's also_known.
        analyst = gc.GitAnalyst()
        commits = [self._commit("h1", "Alice", "a@x.io", "Bob", "b@x.io")]
        analyst._analyze(commits, "r")
        alice_key = "Alice a@x.io"
        bob_key = "Bob b@x.io"
        self.assertIn(bob_key, analyst.persons[alice_key].also_known)
        self.assertIn(alice_key, analyst.persons[bob_key].also_known)


# ---------- CLI source collection ----------

class TestCollectSources(unittest.TestCase):
    def _args(self, **kw):
        defaults = dict(url=None, dir=None, recursive=False, nickname=None, github=False, debug=False)
        defaults.update(kw)
        return types.SimpleNamespace(**defaults)

    def test_no_inputs_returns_empty(self):
        # Regression: original main appended None into repos when -u/-d were
        # absent and silently swallowed them inside append().
        self.assertEqual(gc._collect_sources(self._args()), [])

    def test_url_only(self):
        self.assertEqual(
            gc._collect_sources(self._args(url="https://x/y")),
            ["https://x/y"],
        )

    def test_dir_strips_trailing_slash(self):
        self.assertEqual(
            gc._collect_sources(self._args(dir="/tmp/r/")),
            ["/tmp/r"],
        )

    def test_default_repos_dir(self):
        self.assertEqual(gc.GitAnalyst().repos_dir, gc.DEFAULT_REPOS_DIR)
        self.assertEqual(gc.DEFAULT_REPOS_DIR, "repos")

    def test_custom_repos_dir(self):
        analyst = gc.GitAnalyst(repos_dir="/tmp/custom")
        self.assertEqual(analyst.repos_dir, "/tmp/custom")

    def test_append_uses_configured_repos_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            analyst = gc.GitAnalyst(repos_dir=tmp)
            with mock.patch("gitcolombo.subprocess.run") as run:
                run.return_value = types.SimpleNamespace(
                    returncode=0, stdout="", stderr="",
                )
                analyst.append("https://example.com/u/repo.git")
                clone_call = run.call_args_list[0]
                self.assertEqual(
                    clone_call.args[0],
                    ["git", "clone", "https://example.com/u/repo.git",
                     os.path.join(tmp, "repo")],
                )

    def test_recursive_includes_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a", ".git"))
            sources = gc._collect_sources(self._args(dir=tmp, recursive=True))
            self.assertIn(tmp, sources)
            self.assertIn(os.path.join(tmp, "a"), sources)


# ---------- GitHub login resolution ----------

class TestResolvePersons(unittest.TestCase):
    def test_system_emails_skipped(self):
        analyst = gc.GitAnalyst()
        p = gc.Person(
            key="bot noreply@github.com",
            name="bot", email="noreply@github.com",
            repo_url="https://github.com/u/r", last_commit_hash="abc",
        )
        analyst.persons[p.key] = p
        with mock.patch("gitcolombo.resolve_github_username") as rg:
            analyst.resolve_persons()
            rg.assert_not_called()

    def test_non_github_url_skipped(self):
        with mock.patch("gitcolombo._http_get") as g:
            result = gc.resolve_github_username("https://gitlab.com/u/r", "abc")
            self.assertIsNone(result)
            g.assert_not_called()

    def test_github_login_extracted(self):
        html = b'<a href="/u/r/commits?author=alice42">Alice</a>'
        with mock.patch("gitcolombo._http_get", return_value=html):
            self.assertEqual(
                gc.resolve_github_username("https://github.com/u/r", "abc"),
                "alice42",
            )

    def test_assigns_login_to_person(self):
        analyst = gc.GitAnalyst()
        p = gc.Person(
            key="Alice a@x.io", name="Alice", email="a@x.io",
            repo_url="https://github.com/u/r", last_commit_hash="abc",
        )
        analyst.persons[p.key] = p
        with mock.patch("gitcolombo.resolve_github_username", return_value="alice42"):
            analyst.resolve_persons()
        self.assertEqual(p.github_login, "alice42")


# ---------- End-to-end smoke test with a real repo ----------

@unittest.skipIf(shutil.which("git") is None, "git binary not available")
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Bob",
            "GIT_COMMITTER_EMAIL": "bob@example.com",
        })
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        subprocess.run(
            ["git", "-C", self.tmp, "config", "commit.gpgsign", "false"],
            check=True,
        )
        with open(os.path.join(self.tmp, "f"), "w") as f:
            f.write("hi")
        subprocess.run(["git", "-C", self.tmp, "add", "f"], check=True, env=env)
        subprocess.run(
            ["git", "-C", self.tmp, "commit", "-q", "-m", "first"],
            check=True, env=env,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_analyst_collects_author_and_committer(self):
        analyst = gc.GitAnalyst()
        analyst.append(self.tmp)
        self.assertEqual(len(analyst.commits), 1)
        # Two distinct persons: author and committer.
        names = {p.name for p in analyst.persons.values()}
        self.assertEqual(names, {"Alice", "Bob"})
        # author != committer → mutual also_known link.
        alice = next(p for p in analyst.persons.values() if p.name == "Alice")
        bob = next(p for p in analyst.persons.values() if p.name == "Bob")
        self.assertIn(bob.key, alice.also_known)
        self.assertIn(alice.key, bob.also_known)

    def test_str_render_contains_summary(self):
        analyst = gc.GitAnalyst()
        analyst.append(self.tmp)
        rendered = str(analyst)
        self.assertIn("Verbose persons info", rendered)
        self.assertIn("Total persons: 2", rendered)
        self.assertIn("alice@example.com", rendered)
        self.assertIn("bob@example.com", rendered)


if __name__ == "__main__":
    unittest.main()
