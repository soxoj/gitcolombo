"""Live end-to-end tests for gitcolombo — hit the real GitHub API.

These are intentionally NOT unit tests. They make real HTTP requests to
api.github.com and are skipped automatically in CI (when $CI is set,
which both GitHub Actions and most other runners do by default).

Run locally:
    python3 -m unittest test_gitcolombo_e2e -v
    # optional, much higher rate limits:
    GITHUB_TOKEN=ghp_… python3 -m unittest test_gitcolombo_e2e -v
"""
import os
import unittest

import gitcolombo


IS_CI = bool(os.environ.get("CI"))


@unittest.skipIf(IS_CI, "live network test — skipped in CI ($CI is set)")
class TestGpgKeysLive(unittest.TestCase):
    """/users/{u}/gpg_keys against a real user known to have a verified PGP UID.

    Target: `sindresorhus`. His primary key (key_id D6C7A19E9CFF4BB8, uploaded
    2016-04) lists `sindresorhus@gmail.com` with `verified: true`. The key has
    been stable for years; if GitHub ever returns a different shape or the user
    rotates keys, swap the target rather than weakening the assertion.
    """

    TARGET = "sindresorhus"
    EXPECTED_EMAIL = "sindresorhus@gmail.com"

    def setUp(self):
        self.token = os.environ.get("GITHUB_TOKEN")

    def test_returns_at_least_one_entry(self):
        results = list(gitcolombo.get_gpg_keys_emails(self.TARGET, token=self.token))
        self.assertGreater(
            len(results), 0,
            f"expected at least one GPG email for @{self.TARGET}",
        )

    def test_verified_email_present(self):
        results = list(gitcolombo.get_gpg_keys_emails(self.TARGET, token=self.token))
        verified = {r["email"].lower() for r in results if r["verified"]}
        self.assertIn(
            self.EXPECTED_EMAIL.lower(), verified,
            f"expected verified UID {self.EXPECTED_EMAIL} in {sorted(verified)}",
        )

    def test_entry_shape(self):
        results = list(gitcolombo.get_gpg_keys_emails(self.TARGET, token=self.token))
        match = next(
            (r for r in results if r["email"].lower() == self.EXPECTED_EMAIL.lower()),
            None,
        )
        self.assertIsNotNone(match, "expected entry not found")
        self.assertTrue(match["verified"])
        self.assertIn(match["source"], ("primary", "subkey"))
        self.assertTrue(match["key_id"], "key_id should not be empty")


@unittest.skipIf(IS_CI, "live network test — skipped in CI ($CI is set)")
class TestTrailersLive(unittest.TestCase):
    """Verify trailer parsing against /search/commits results for a kernel maintainer.

    Linux kernel commits ALWAYS have `Signed-off-by:` (DCO is enforced),
    and most also carry one or more of `Acked-by:`, `Reviewed-by:`,
    `Tested-by:`, `Reported-by:`, `Cc:` from the patch-review chain.
    These tests assert that the extractor surfaces ≥ 2 distinct trailer
    types from a single `--search gregkh` run, plus a specific check for
    `signed-off-by` which is guaranteed by DCO.

    Target: gregkh (Greg Kroah-Hartman). His commit pool on GitHub
    spans hundreds of repos (including kernel mirrors), so /search/commits
    will return diverse trailer types regardless of GitHub indexing
    fluctuations.
    """

    TARGET = "gregkh"

    TRAILER_KEYS = frozenset({
        "signed-off-by", "co-authored-by", "reviewed-by", "tested-by",
        "reported-by", "acked-by", "suggested-by", "cc",
    })

    @classmethod
    def setUpClass(cls):
        token = os.environ.get("GITHUB_TOKEN")
        cls.results = list(gitcolombo.search_commits_by_author(
            cls.TARGET, token=token,
        ))
        cls.roles_seen = {r["role"] for r in cls.results}
        cls.trailer_roles = cls.roles_seen & cls.TRAILER_KEYS

    def test_collected_some_identities(self):
        self.assertGreater(
            len(self.results), 50,
            "expected dozens of identities from kernel maintainer's commits",
        )

    def test_signed_off_by_present(self):
        # DCO-enforced project — Signed-off-by must appear.
        self.assertIn("signed-off-by", self.trailer_roles)

    def test_at_least_two_distinct_trailer_types(self):
        self.assertGreaterEqual(
            len(self.trailer_roles), 2,
            f"expected ≥2 trailer kinds, saw {sorted(self.trailer_roles)}",
        )

    def test_trailer_entries_have_email_and_role(self):
        trailer_entries = [r for r in self.results if r["role"] in self.TRAILER_KEYS]
        self.assertGreater(len(trailer_entries), 0)
        for entry in trailer_entries[:5]:
            self.assertTrue(entry["email"])
            self.assertIn("@", entry["email"])
            self.assertTrue(entry["repo"])


class TestSystemEmailFilter(unittest.TestCase):
    """is_system_email() classifies vendor noreply addresses across domains."""

    SYSTEM = [
        "noreply@github.com",
        "noreply@anthropic.com",      # Claude Code Co-Authored-By trailer
        "no-reply@gitlab.com",
        "donotreply@example.org",
        "DoNotReply@example.org",      # case-insensitive
        "12345+ghost@users.noreply.github.com",
    ]

    REAL = [
        "alice@example.com",
        "torvalds@linux-foundation.org",
        "noreply-thing@example.com",   # not a noreply, just starts with noreply-
        "support@noreply.example.com", # noreply is in the domain but mailbox is real
    ]

    def test_system_emails_classified(self):
        for e in self.SYSTEM:
            self.assertTrue(gitcolombo.is_system_email(e), f"expected system: {e}")

    def test_real_emails_not_classified(self):
        for e in self.REAL:
            self.assertFalse(gitcolombo.is_system_email(e), f"expected real: {e}")


class TestTrailerRegex(unittest.TestCase):
    """Offline sanity check on the trailer regex.

    Not strictly e2e — but a fast deterministic guard that the regex
    can extract every supported trailer type from a canonical message.
    Runs in CI too (no network).
    """

    SAMPLE_MESSAGE = (
        "usb: serial: cp210x: add support for a new device\n"
        "\n"
        "This patch adds support for the newly released widget.\n"
        "\n"
        "Reported-by: Foo Bar <foo@bar.com>\n"
        "Tested-by: Tester One <t1@example.org>\n"
        "Reviewed-by: Bob Reviewer <bob@example.org>\n"
        "Acked-by: Carol Maintainer <carol@example.net>\n"
        "Cc: stable@vger.kernel.org <stable@vger.kernel.org>\n"
        "Co-authored-by: Alice Coder <alice@nowhere.dev>\n"
        "Suggested-by: Dave Idea <dave@ideas.io>\n"
        "Signed-off-by: Greg Kroah-Hartman <gregkh@linuxfoundation.org>\n"
    )

    def test_all_trailer_types_match(self):
        keys = {m.group("key").lower()
                for m in gitcolombo.TRAILER_RE.finditer(self.SAMPLE_MESSAGE)}
        expected = {
            "signed-off-by", "co-authored-by", "reviewed-by", "tested-by",
            "reported-by", "acked-by", "suggested-by", "cc",
        }
        self.assertEqual(keys, expected)

    def test_email_extraction(self):
        emails = {m.group("email").lower()
                  for m in gitcolombo.TRAILER_RE.finditer(self.SAMPLE_MESSAGE)}
        self.assertIn("gregkh@linuxfoundation.org", emails)
        self.assertIn("alice@nowhere.dev", emails)
        self.assertIn("foo@bar.com", emails)

    def test_no_match_on_plain_prose(self):
        # "Signed-off-by:" mentioned in body text without proper format must not match
        prose = "We use Signed-off-by lines in this project but here it's just text."
        self.assertEqual(list(gitcolombo.TRAILER_RE.finditer(prose)), [])

    def test_rejects_crammed_line_with_mention_and_label(self):
        # Regression: a Linux kernel commit body line of the form
        #   Reported-by: Foo Bar (@kaitas) Signed-off-by: Linus <linus@k.org>
        # used to be captured with the entire mush as the "name". The lazy
        # regex now still matches, but the post-filter on ':' / '@' must drop
        # the entry instead of producing the malformed name.
        from gitcolombo import search_commits_by_author  # noqa: F401
        msg = (
            "Reported-by: Foo Bar (@kaitas) Signed-off-by: Linus Torvalds "
            "<linus@kernel.org>\n"
        )
        # simulate the post-filter step used inside search_commits_by_author
        captured = []
        for tm in gitcolombo.TRAILER_RE.finditer(msg):
            name = (tm.group("name") or "").strip()
            if ":" in name or "@" in name:
                continue
            captured.append(name)
        self.assertEqual(captured, [], f"should have rejected, got: {captured}")


@unittest.skipIf(IS_CI, "live network test — skipped in CI ($CI is set)")
class TestGpgKeysNoResult(unittest.TestCase):
    """A user without uploaded PGP keys must yield zero results, not crash.

    Target: `GONZOsint` — verified during the methodology audit to have 0 keys.
    """

    def test_empty_result_for_user_without_keys(self):
        token = os.environ.get("GITHUB_TOKEN")
        results = list(gitcolombo.get_gpg_keys_emails("GONZOsint", token=token))
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
