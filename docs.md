# Extraction methods

This document describes how the standalone web tool
(`gitcolombo.html` — a single file, open it in a browser) and the
Python CLI (`gitcolombo.py`) discover identities (name + email pairs)
from GitHub.

All methods rely on the public GitHub REST API (no auth required, but a
personal access token raises the rate limit from 60 to 5000 requests
per hour).

---

## Mode A — Find real email by username

Goal: given a GitHub username, return a ranked list of `(email, name)`
identities that most likely belong to the real person behind the
account.

This is the same general approach taken by
[gitrecon](https://github.com/GONZOsint/gitrecon).

### Step 1 — Profile lookup

```
GET https://api.github.com/users/{username}
```

We read `name`, `login`, `public_repos`, `created_at`. The `name`
field is later used to rank identities — an email paired with a
commit-author name that matches the profile name is treated as
high-confidence.

### Step 2 — PGP keys (strongest signal)

```
GET https://api.github.com/users/{username}/gpg_keys
```

Returns the PGP public keys the user has uploaded to GitHub. Each
key (and each subkey) exposes its UIDs as a list of objects:

```json
[
  {
    "key_id": "D6C7A19E9CFF4BB8",
    "revoked": false,
    "emails": [
      {"email": "real@example.com", "verified": true}
    ],
    "subkeys": [
      {
        "key_id": "1053D098A0070FEA",
        "revoked": false,
        "emails": [...]
      }
    ]
  }
]
```

Why this is the strongest signal in this tool:

- The user uploaded the key themselves — this is direct identity disclosure, not
  inferred from commit metadata.
- `verified: true` means GitHub sent a confirmation email to that address and the
  user clicked the link. This is **cryptographically and procedurally verified**,
  unlike `commit.author.email`, which is freely settable in `git config user.email`.
- The endpoint is public, no auth required, and unaffected by "Keep my email
  address private".

We walk both top-level keys and their `subkeys[]`, skipping any with
`revoked: true`. Emails are deduplicated against later sources, so a
match between a PGP UID and a commit email lights up both signals and
boosts ranking.

Limitations: many users never upload a PGP key. None of the surveyed
peer tools (gitrecon, EmailFinder, GitFive, Gitmails, etc.) use this
endpoint, so it is a unique source.

### Step 3 — Public events (recent commits)

```
GET https://api.github.com/users/{username}/events/public?per_page=100
```

The `/events/public` endpoint returns up to ~90 days of the user's
public activity. Each entry has a `type` and a `payload`. We are only
interested in `PushEvent`:

```json
{
  "type": "PushEvent",
  "repo": { "name": "owner/repo" },
  "payload": {
    "commits": [
      {
        "sha": "...",
        "author": { "email": "real@example.com", "name": "Real Name" },
        "message": "..."
      }
    ]
  }
}
```

For every `PushEvent.payload.commits[]` we collect
`author.email` + `author.name`.

Why this works: when the user pushes from their local machine, git
stamps the commit with whatever email is configured locally
(`user.email`). GitHub keeps that email in the public event feed even
if the user later enables "Keep my email address private" in the web
UI — events that already happened are not rewritten.

### Step 4 — Commit search across all public repos

```
GET https://api.github.com/search/commits?q=author:{username}&per_page=100&page={n}
```

Unlike `/events/public`, the commit-search index spans **every public
commit on GitHub** that has been associated (by email or login) with the
target account, including commits in repositories the user does not
own. It also surfaces old commits that fell out of the 90-day events
window.

The endpoint caps results at 1000 commits = 10 pages of 100. Rate limit
is stricter than core: 10 req/min unauthenticated, 30 req/min with a
token (vs 5000/hr for core endpoints).

For each `item` we read both the author/committer pair from
`item.commit.{author, committer}` and parse trailers from
`item.commit.message` (see the "Commit-message trailer extraction"
section).

This is the same approach GitFive uses for its "lite" search mode and
the reason it surfaces emails that older tools miss.

### Step 5 — Commits fallback

If the steps above returned nothing useful (e.g. the user has been
inactive for more than ~90 days, or every event email is
`@users.noreply.github.com`), we fall back to scanning commits in
the user's own repositories:

```
GET https://api.github.com/users/{username}/repos?per_page=100&sort=updated
```

We keep non-fork repositories and, for each one, query:

```
GET https://api.github.com/repos/{owner}/{repo}/commits?author={username}&per_page=100
```

For every returned commit we read both author and committer:

- `commit.author.email` / `commit.author.name`
- `commit.committer.email` / `commit.committer.name`

The `?author=` filter on the commits endpoint accepts either a
GitHub login or an email — using the login restricts results to
commits GitHub has actually linked to this account.

### Step 6 — Ranking

Each unique `(email, name)` pair is annotated with:

- `count` — how often it was seen
- `sources` — which events/repos it came from
- `matchesProfile` — whether `name` (case-insensitive, trimmed) equals
  the profile `name` from Step 1

Identities where `matchesProfile === true` are shown first, then
remaining ones sorted by `count` descending.

---

## Mode B — Extract emails from commits

Goal: dump every `(email, login)` pair that appears as a commit author
across a user's or org's repositories — useful for mapping a team or
auditing a single repo.

### Step 1 — Resolve target

The input is either `username` or `owner/repo`.

For an `owner/repo`:

```
GET https://api.github.com/repos/{owner}/{repo}
```

For a `username`, we paginate through:

```
GET https://api.github.com/users/{username}/repos?per_page=100&page={n}
```

Forks are skipped when the **Skip forks** checkbox is on (default).

### Step 2 — Walk commits

For every repository:

```
GET https://api.github.com/repos/{owner}/{repo}/commits?per_page=100&page={n}
```

For every commit object:

```json
{
  "sha": "...",
  "commit": {
    "author":    { "email": "alice@example.com", "name": "Alice" },
    "committer": { "email": "bob@example.com",   "name": "Bob" }
  },
  "author":    { "login": "alice42" },
  "committer": { "login": "bob42" }
}
```

We collect:

- `commit.author.email` + top-level `author.login`
- `commit.committer.email` + top-level `committer.login`

(Top-level `author.login` is GitHub's resolved account for the commit;
it may be `null` for commits whose email isn't tied to any GitHub
account.)

Pagination stops when the API returns an empty array. When
**Only first 100 commits/repo** is enabled, only page 1 is requested.

### Step 3 — Dedupe

Identities are deduplicated by lowercased email. Duplicates increment
a `count` field instead of producing new rows.

---

## Commit-message trailer extraction

For every commit body the tool sees (whether from `/events`,
`/search/commits`, or `/repos/{owner}/{repo}/commits`), it scans the
message for well-known git trailers and treats each match as an
additional identity. The regex is anchored per line and matches:

```
^(Signed-off-by|Co-authored-by|Reviewed-by|Tested-by|Reported-by|Acked-by|Suggested-by|Cc):\s+(name)\s+<(email)>$
```

| Trailer | Where it usually comes from |
|---|---|
| `Signed-off-by:` | `git commit -s` — DCO projects (Linux kernel, Docker, kubernetes, many enterprise projects with CLAs) |
| `Co-authored-by:` | GitHub's UI when squash-merging a PR with multiple contributors |
| `Reviewed-by:` | Patch-review workflows (kernel, libvirt, qemu, …) |
| `Tested-by:` / `Acked-by:` / `Reported-by:` / `Suggested-by:` | Kernel-style review chains |
| `Cc:` | Notification chain in mailing-list patches |

Why this is a strong signal: trailer lines are added **intentionally**
by tooling or by humans during patch review, and they typically point
to real mailboxes (no point in CC-ing or signing-off as a fake
address — it defeats the workflow's purpose). Even when the commit
author's email is a noreply, the trailers often carry plain emails.

Tool comparison: among the 14 surveyed peer tools, only `fue` (Ruby,
GraphQL-based) and `GitFive` parse these trailers. Gitcolombo's
implementation mirrors fue's regex while staying fully in-browser
(Mode A + Mode B) and in the Python CLI's `--search` flow.

Each trailer email is tagged with a `trailer` badge in the UI (hover
to see the trailer types it appeared in). The Python CLI prints the
roles for each identity in square brackets, e.g.
`[signed-off-by, reviewed-by, author]`.

## Filters

### Ignore `@users.noreply.github.com`

When the checkbox is enabled, any email matching the regular
expression `/@users\.noreply\.github\.com$/i` is dropped before being
recorded. This is the address GitHub generates for users who turn on
"Keep my email address private" — it is *not* a real mailbox and
matching it back to a person requires extracting the leading numeric
prefix (the user's internal numeric id) which doesn't directly reveal
their identity.

These emails are still useful as a signal that the user is privacy-conscious,
which is why the option is a toggle, not a hard filter.

### Skip forks (Mode B only)

Fork repositories are noisy: their commit history is dominated by the
upstream project, not the user being investigated. The checkbox is on
by default.

### Limit to first 100 commits/repo (Mode B only)

Each repo's commit list is paginated. Stopping after page 1 keeps the
total request count predictable; turn this off if you need full
coverage on small projects.

---

## Rate limits

GitHub allows 60 unauthenticated requests per hour per IP and 5000
requests per hour per authenticated token. The tool surfaces a clear
message on `403`/`429` responses and shows whatever partial results
were already collected.

A token only needs the default `public_repo` read scope — paste a
fine-grained PAT with no extra permissions and you're set.

---

## References

- gitrecon — <https://github.com/GONZOsint/gitrecon>
- ghintel — <https://ghintel.secrets.ninja/> (UI inspiration)
- GitHub REST API — <https://docs.github.com/en/rest>
