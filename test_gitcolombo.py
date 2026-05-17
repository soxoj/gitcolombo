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
        defaults = dict(
            url=None, dir=None, recursive=False, nickname=None,
            github=False, debug=False, include_forks=False,
            clone_workers=gc.CLONE_WORKERS, no_color=False,
        )
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
        self.assertIn("[ identities ]", rendered)
        self.assertIn("[ stats ]", rendered)
        self.assertRegex(rendered, r"persons\s+\.+\s+2")
        self.assertIn("alice@example.com", rendered)
        self.assertIn("bob@example.com", rendered)


# ---------- Console styling helpers ----------

class TestStylingHelpers(unittest.TestCase):
    """Covers _setup_colors / _c / _email_with_tag / _email_brackets / _section.

    Color state is module-level; save and restore it around each test so
    one test cannot leak ANSI codes into another's assertions.
    """

    def setUp(self):
        self._prev = gc._COLOR_ENABLED
        gc._COLOR_ENABLED = False

    def tearDown(self):
        gc._COLOR_ENABLED = self._prev

    def test_c_returns_plain_when_colors_disabled(self):
        # Defensive: even when called with an ANSI code, plain text is emitted
        # when colors are off — otherwise piping to a file dumps escape codes.
        self.assertEqual(gc._c(gc.NEON, "hello"), "hello")
        self.assertNotIn("\033", gc._c(gc.NEON, "hello"))

    def test_c_wraps_with_reset_when_colors_enabled(self):
        gc._COLOR_ENABLED = True
        out = gc._c(gc.NEON, "hello")
        self.assertTrue(out.startswith(gc.NEON))
        self.assertTrue(out.endswith(gc.RESET))
        self.assertIn("hello", out)

    def test_setup_colors_force_off_overrides_tty(self):
        gc._setup_colors(force_off=True)
        self.assertFalse(gc._COLOR_ENABLED)

    def test_setup_colors_respects_no_color_env(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            gc._setup_colors(force_off=False)
        self.assertFalse(gc._COLOR_ENABLED)

    def test_setup_colors_off_for_non_tty(self):
        # Plain stdout (e.g. captured by unittest) is not a TTY → no colors.
        with mock.patch.dict(os.environ, {}, clear=True):
            gc._setup_colors(force_off=False)
        self.assertFalse(gc._COLOR_ENABLED)

    def test_email_with_tag_marks_noreply(self):
        out = gc._email_with_tag("noreply@github.com")
        self.assertIn("noreply@github.com", out)
        self.assertIn("[noreply]", out)

    def test_email_with_tag_skips_normal_email(self):
        self.assertEqual(gc._email_with_tag("alice@example.com"),
                         "alice@example.com")

    def test_email_brackets_keeps_tag_outside_angle_brackets(self):
        # Regression: an earlier version stuck [noreply] INSIDE the <...>
        # which produced things like `<noreply@github.com [noreply]>`.
        out = gc._email_brackets("noreply@github.com")
        self.assertTrue(out.startswith("<"))
        bracket_close = out.index(">")
        self.assertLess(bracket_close, out.index("[noreply]"))

    def test_email_brackets_plain_email_has_no_tag(self):
        self.assertEqual(gc._email_brackets("alice@example.com"),
                         "<alice@example.com>")

    def test_section_returns_header_with_two_rules(self):
        block = gc._section("identities")
        # _section yields: blank, rule, "[ identities ]", rule, blank
        self.assertEqual(len(block), 5)
        self.assertIn("[ identities ]", block[2])
        # The two rule lines on either side of the title are identical.
        self.assertEqual(block[1], block[3])


# ---------- Person.__str__ rendering ----------

class TestPersonRender(unittest.TestCase):
    def setUp(self):
        self._prev = gc._COLOR_ENABLED
        gc._COLOR_ENABLED = False

    def tearDown(self):
        gc._COLOR_ENABLED = self._prev

    def test_header_uses_arrow_and_brackets(self):
        p = gc.Person(key="Alice a@x.io", name="Alice", email="a@x.io",
                      as_author=3)
        out = str(p)
        first = out.splitlines()[0]
        self.assertIn("▶", first)
        self.assertIn("Alice", first)
        self.assertIn("<a@x.io>", first)

    def test_counts_use_times_n_notation(self):
        p = gc.Person(key="A a@x", name="A", email="a@x",
                      as_author=11, as_committer=7)
        out = str(p)
        self.assertIn("×11", out)
        self.assertIn("×7", out)

    def test_last_row_uses_l_branch(self):
        # Single row → final branch char `└─`, not `├─`.
        p = gc.Person(key="A a@x", name="A", email="a@x", as_author=1)
        rows = [line for line in str(p).splitlines() if "author" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("└─", rows[0])

    def test_two_rows_use_tee_then_l(self):
        p = gc.Person(key="A a@x", name="A", email="a@x",
                      as_author=1, as_committer=1)
        body = str(p).splitlines()[1:]
        self.assertIn("├─", body[0])
        self.assertIn("└─", body[-1])

    def test_noreply_tag_on_alias_email_outside_brackets(self):
        # Alias whose email is a noreply service address should render
        # `<noreply@github.com> [noreply]` — tag after the closing `>`.
        alias = gc.Person(key="b", name="GitHub", email="noreply@github.com")
        p = gc.Person(key="a", name="Alice", email="a@x.io", as_author=1)
        p.also_known[alias.key] = alias
        out = str(p)
        # The alias row must contain both `>` and `[noreply]` with the tag
        # appearing after the closing angle bracket.
        alias_line = next(l for l in out.splitlines() if "GitHub" in l)
        self.assertLess(alias_line.index(">"), alias_line.index("[noreply]"))

    def test_verified_tag_appears_for_resolved_login(self):
        p = gc.Person(key="A a@x", name="A", email="a@x", as_author=1,
                      github_login="alice42")
        out = str(p)
        self.assertIn("https://github.com/alice42", out)
        self.assertIn("[verified]", out)


# ---------- GitAnalyst.__str__ rendering ----------

class TestAnalystRender(unittest.TestCase):
    def setUp(self):
        self._prev = gc._COLOR_ENABLED
        gc._COLOR_ENABLED = False

    def tearDown(self):
        gc._COLOR_ENABLED = self._prev

    def _commit(self, h, a_name, a_email, c_name=None, c_email=None):
        c_name = c_name or a_name
        c_email = c_email or a_email
        line = f'{h};"{a_name} {a_email}";"{c_name} {c_email}"'
        return gc.Commit.parse(line)

    def _filled(self):
        analyst = gc.GitAnalyst()
        analyst.repos = ["/tmp/r1", "/tmp/r2"]
        analyst.commits = [self._commit("h1", "Alice", "a@x.io"),
                           self._commit("h2", "Bob", "b@x.io")]
        analyst._analyze(analyst.commits, "/tmp/r1")
        return analyst

    def test_section_ordering_stats_first(self):
        # User-facing requirement: stats above correlation above identities.
        a = self._filled()
        # Force a correlation block to exist via shared email set.
        a._analyze(
            [self._commit("h3", "Alice", "shared@x.io"),
             self._commit("h4", "AliceB", "shared@x.io")],
            "/tmp/r1",
        )
        out = str(a)
        i_stats = out.index("[ stats ]")
        i_corr = out.index("[ correlation ]")
        i_idents = out.index("[ identities ]")
        self.assertLess(i_stats, i_corr)
        self.assertLess(i_corr, i_idents)

    def test_stats_uses_dot_leaders(self):
        out = str(self._filled())
        # `persons ........ N` with dots between label and value.
        self.assertRegex(out, r"persons\s+\.+\s+\d+")
        self.assertRegex(out, r"commits\s+\.+\s+\d+")
        self.assertRegex(out, r"repos\s+\.+\s+\d+")

    def test_targets_listed_as_tree(self):
        out = str(self._filled())
        # The last target uses `└─`, earlier ones `├─`.
        target_lines = [l for l in out.splitlines() if "/tmp/r" in l]
        self.assertEqual(len(target_lines), 2)
        self.assertIn("├─", target_lines[0])
        self.assertIn("└─", target_lines[1])

    def test_correlation_absent_when_no_overlap(self):
        # Pure disjoint identities → no correlation section.
        out = str(self._filled())
        self.assertNotIn("[ correlation ]", out)

    def test_correlation_present_for_shared_name_two_emails(self):
        analyst = gc.GitAnalyst()
        analyst.repos = ["/tmp/r"]
        commits = [
            self._commit("h1", "Alice", "a1@x.io"),
            self._commit("h2", "Alice", "a2@x.io"),
        ]
        analyst.commits = commits
        analyst._analyze(commits, "/tmp/r")
        out = str(analyst)
        self.assertIn("[ correlation ]", out)
        # The N→emails summary uses `→` and the count.
        self.assertRegex(out, r"Alice\s*→\s*2 emails")

    def test_correlation_lists_same_person_clusters(self):
        analyst = gc.GitAnalyst()
        analyst.repos = ["/tmp/r"]
        commits = [
            self._commit("h1", "Alice", "shared@x.io"),
            self._commit("h2", "AliceB", "shared@x.io"),
        ]
        analyst.commits = commits
        analyst._analyze(commits, "/tmp/r")
        out = str(analyst)
        self.assertIn("same person", out)
        self.assertIn("Alice", out)
        self.assertIn("AliceB", out)
        # The cluster line uses ≡ to join names.
        self.assertIn("≡", out)


# ---------- clone_many parallel cloning ----------

class TestCloneMany(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        with mock.patch("gitcolombo.subprocess.run") as run:
            self.assertEqual(gc.clone_many([], "/tmp/x"), {})
            run.assert_not_called()

    def _stub_clone_factory(self, dest_dir, *, fail_urls=()):
        """subprocess.run replacement that creates a fake repo dir on success."""
        def fake_run(argv, **kw):
            url = argv[2]
            target = argv[3]
            if url in fail_urls:
                return types.SimpleNamespace(returncode=128, stdout="",
                                             stderr="boom")
            os.makedirs(os.path.join(target, ".git"), exist_ok=True)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake_run

    def test_returns_url_to_path_mapping_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = [
                "https://github.com/u/a",
                "https://github.com/u/b",
                "https://github.com/u/c",
            ]
            with mock.patch("gitcolombo.subprocess.run",
                            side_effect=self._stub_clone_factory(tmp)):
                results = gc.clone_many(urls, tmp, workers=4)
            self.assertEqual(set(results.keys()), set(urls))
            for url, path in results.items():
                self.assertIsNotNone(path, f"{url} should have a path")
                self.assertTrue(os.path.isdir(os.path.join(path, ".git")))

    def test_failed_clones_map_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = ["https://github.com/u/ok", "https://github.com/u/bad"]
            fake = self._stub_clone_factory(
                tmp, fail_urls={"https://github.com/u/bad"},
            )
            with mock.patch("gitcolombo.subprocess.run", side_effect=fake):
                results = gc.clone_many(urls, tmp, workers=2)
            self.assertIsNotNone(results["https://github.com/u/ok"])
            self.assertIsNone(results["https://github.com/u/bad"])

    def test_runs_one_subprocess_per_url(self):
        # Sanity: parallelization must not change the total work performed.
        with tempfile.TemporaryDirectory() as tmp:
            urls = [f"https://github.com/u/r{i}" for i in range(5)]
            with mock.patch("gitcolombo.subprocess.run",
                            side_effect=self._stub_clone_factory(tmp)) as run:
                gc.clone_many(urls, tmp, workers=3)
            # 5 URLs => 5 `git clone` invocations, regardless of worker count.
            self.assertEqual(run.call_count, 5)

    def test_progress_written_to_stderr_not_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = ["https://github.com/u/a"]
            import io
            err = io.StringIO()
            with mock.patch("gitcolombo.subprocess.run",
                            side_effect=self._stub_clone_factory(tmp)), \
                 mock.patch("sys.stderr", err):
                gc.clone_many(urls, tmp, workers=1)
            self.assertIn("cloning", err.getvalue())
            self.assertIn("1/1", err.getvalue())


# ---------- GitAnalyst.append with pre-cloned path ----------

class TestAppendClonedPath(unittest.TestCase):
    def test_cloned_path_bypasses_git_clone(self):
        # Regression: clone_many pre-clones URLs in parallel and feeds the
        # local path back into append(); we must not re-clone.
        with tempfile.TemporaryDirectory() as tmp:
            analyst = gc.GitAnalyst(repos_dir=tmp)
            with mock.patch("gitcolombo.git_clone") as clone, \
                 mock.patch("gitcolombo.git_log", return_value=""):
                analyst.append("https://github.com/u/r", cloned_path=tmp)
                clone.assert_not_called()
            self.assertEqual(analyst.repos, [tmp])

    def test_source_url_preserved_for_login_resolution(self):
        # When append is called with cloned_path, the original URL is still
        # passed to _analyze as repo_url so resolve_github_username can use it.
        with tempfile.TemporaryDirectory() as tmp:
            analyst = gc.GitAnalyst(repos_dir=tmp)
            captured = {}

            def fake_analyze(commits, repo_url):
                captured["repo_url"] = repo_url

            with mock.patch.object(analyst, "_analyze", side_effect=fake_analyze), \
                 mock.patch("gitcolombo.git_log", return_value=""):
                analyst.append("https://github.com/u/r", cloned_path=tmp)
            self.assertEqual(captured["repo_url"], "https://github.com/u/r")


# ---------- get_github_repos transparency ----------

class TestGetGithubReposLogging(unittest.TestCase):
    """The 245→31 confusion was caused by silent fork-skipping and silent
    page-fetch failures. The function now logs a summary so the user can
    explain the gap without --debug.
    """

    def test_logs_listing_summary_with_fork_count(self):
        page = [
            {"html_url": "https://github.com/u/keep1", "fork": False},
            {"html_url": "https://github.com/u/keep2", "fork": False},
            {"html_url": "https://github.com/u/fork1", "fork": True},
            {"html_url": "https://github.com/u/fork2", "fork": True},
            {"html_url": "https://github.com/u/fork3", "fork": True},
        ]
        with mock.patch("gitcolombo._http_get_json", return_value=page), \
             self.assertLogs("gitcolombo", level="INFO") as cm:
            kept = gc.get_github_repos("u", repos_count=5)
        joined = "\n".join(cm.output)
        self.assertIn("5 seen", joined)
        self.assertIn("3 forks skipped", joined)
        self.assertEqual(len(kept), 2)

    def test_logs_kept_wording_when_include_forks(self):
        page = [
            {"html_url": "https://github.com/u/r1", "fork": False},
            {"html_url": "https://github.com/u/r2", "fork": True},
        ]
        with mock.patch("gitcolombo._http_get_json", return_value=page), \
             self.assertLogs("gitcolombo", level="INFO") as cm:
            gc.get_github_repos("u", repos_count=2, include_forks=True)
        joined = "\n".join(cm.output)
        self.assertIn("forks kept", joined)

    def test_warns_on_failed_page(self):
        # _http_get_json returns None when the API call fails (rate limit,
        # network error, etc.). The user must see this, not have repos
        # silently disappear.
        with mock.patch("gitcolombo._http_get_json", return_value=None), \
             self.assertLogs("gitcolombo", level="WARNING") as cm:
            gc.get_github_repos("u", repos_count=100)
        joined = "\n".join(cm.output)
        self.assertIn("returned no data", joined)
        self.assertIn("rate limit", joined)

    def test_collect_sources_passes_include_forks_flag(self):
        args = types.SimpleNamespace(
            url=None, dir=None, recursive=False, nickname="u",
            github=False, debug=False, include_forks=True,
            clone_workers=gc.CLONE_WORKERS, no_color=False,
        )
        with mock.patch("gitcolombo.get_public_repos_count", return_value=1), \
             mock.patch("gitcolombo.get_github_repos") as gh:
            gh.return_value = set()
            gc._collect_sources(args)
            gh.assert_called_once()
            self.assertTrue(gh.call_args.kwargs["include_forks"])


if __name__ == "__main__":
    unittest.main()
