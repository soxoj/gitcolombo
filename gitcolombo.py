#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import subprocess
from threading import Thread
import urllib.request


DELIMITER = '---------------'

LOG_FORMAT = r'%H;"%an %ae";"%cn %ce"'
LOG_REGEXP = r'(\w+);"(.*?)";"(.*?)"'
LOG_NAME_REGEXP = r'^(.*?)\s+(\S+)$'

GIT_EXTRACT_CMD = "git log --pretty='{}' --all".format(LOG_FORMAT)
GIT_CLONE_CMD = "git clone {}"

GITHUB_USER_STATS = 'https://api.github.com/users/{}'
GITHUB_USER_REPOS = 'https://api.github.com/users/{}/repos?per_page=100&page={}'
GITHUB_USER_GPG_KEYS = 'https://api.github.com/users/{}/gpg_keys'
GITHUB_SEARCH_COMMITS = 'https://api.github.com/search/commits?q=author:{}&per_page=100&page={}'
GITHUB_PER_PAGE_LIMIT = 100
GITHUB_SEARCH_MAX_PAGES = 10  # /search/* caps results at 1000

# Well-known git trailer keys (DCO sign-off, GitHub co-authorship, kernel reviews).
# A real email in any of these is a strong identity signal: trailers are
# typically added intentionally by tooling (`git commit -s`, GitHub UI's
# "Co-authored-by", patch-review workflows) rather than being auto-generated.
TRAILER_RE = re.compile(
    r'^(?P<key>Signed-off-by|Co-authored-by|Reviewed-by|Tested-by|Reported-by|Acked-by|Suggested-by|Cc):\s+(?P<name>[^<]+?)\s+<(?P<email>[^>]+)>\s*$',
    re.MULTILINE | re.IGNORECASE,
)

SYSTEM_EMAILS = [
    'noreply@github.com',
]

# Service noreply addresses from any vendor (github, anthropic, gitlab, ...)
# plus GitHub's user-private `{id}+{login}@users.noreply.github.com` pattern.
SYSTEM_EMAIL_RE = re.compile(
    r'(^(?:noreply|no-reply|donotreply|do-not-reply)@|@users\.noreply\.github\.com$)',
    re.IGNORECASE,
)


def is_system_email(email):
    return bool(email and SYSTEM_EMAIL_RE.search(email))


def get_public_repos_count(nickname):
    url = GITHUB_USER_STATS
    req_url = url.format(nickname)
    req = urllib.request.Request(req_url)
    try:
        response = urllib.request.urlopen(req)
    except Exception as e:
        logging.debug(e)
    else:
        stats = json.loads((response.read().decode('utf8')))
        repos_count = stats["public_repos"]
        if repos_count:
            return repos_count


def get_github_repos(nickname, only_forks=True, repos_count=GITHUB_PER_PAGE_LIMIT):
    repos_links = set()
    if not repos_count:
        return repos_links
    url = GITHUB_USER_REPOS
    last_page = int(repos_count / GITHUB_PER_PAGE_LIMIT) + (repos_count % GITHUB_PER_PAGE_LIMIT > 0)
    for page_num in range(1, last_page + 1):
        req_url = url.format(nickname, page_num)
        req = urllib.request.Request(req_url)
        try:
            response = urllib.request.urlopen(req)
        except Exception as e:
            logging.debug(e)
        else:
            repos = json.loads((response.read().decode('utf8')))
            result = [r['html_url'] for r in repos if not only_forks or not r['fork']]
            repos_links.update(set(result))

    return repos_links


def get_gpg_keys_emails(nickname, token=None):
    """Fetch user-uploaded PGP keys via /users/{u}/gpg_keys and yield emails.

    These emails come from the key's UIDs — the user uploaded them themselves,
    so this is a direct identity disclosure. `verified=True` means GitHub has
    confirmed the user controls that mailbox.

    Yields dicts: {email, verified, key_id, created_at, source ('primary'|'subkey')}.
    """
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'gitcolombo',
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req_url = GITHUB_USER_GPG_KEYS.format(nickname)
    req = urllib.request.Request(req_url, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        logging.debug('gpg_keys %s: %s', req_url, e)
        return
    try:
        keys = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        logging.debug('gpg_keys parse error: %s', e)
        return
    seen = set()

    def _walk(key, source):
        if not key or key.get('revoked'):
            return
        key_id = key.get('key_id', '')
        created = key.get('created_at', '')
        for entry in (key.get('emails') or []):
            email = entry.get('email')
            if not email:
                continue
            k = email.lower()
            if k in seen:
                continue
            seen.add(k)
            yield {
                'email': email,
                'verified': bool(entry.get('verified')),
                'key_id': key_id,
                'created_at': created,
                'source': source,
            }

    for key in (keys or []):
        for row in _walk(key, 'primary'):
            yield row
        for sub in (key.get('subkeys') or []):
            for row in _walk(sub, 'subkey'):
                yield row


def print_gpg_results(results, ignore_noreply=True):
    """Pretty-print get_gpg_keys_emails() output."""
    rows = [
        r for r in results
        if not (ignore_noreply and is_system_email(r['email']))
    ]
    if not rows:
        return False
    print('PGP key UIDs (uploaded by the user, public via /users/{u}/gpg_keys):')
    print(DELIMITER)
    # verified first
    rows.sort(key=lambda r: (not r['verified'], r['email']))
    for r in rows:
        flag = 'verified' if r['verified'] else 'unverified'
        print('  {:40}  [{}]  key_id={}  ({})'.format(
            r['email'], flag, r['key_id'] or '?', r['source']
        ))
    print()
    return True


def search_commits_by_author(nickname, token=None):
    """Use /search/commits?q=author:{u} to find commits across all of public GitHub.

    Yields dicts: {email, name, role ('author'|'committer'), repo, sha, date}.
    """
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'gitcolombo',
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
    seen = set()
    for page in range(1, GITHUB_SEARCH_MAX_PAGES + 1):
        req_url = GITHUB_SEARCH_COMMITS.format(nickname, page)
        req = urllib.request.Request(req_url, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            logging.debug('search/commits %s: %s', req_url, e)
            return
        try:
            data = json.loads(response.read().decode('utf-8'))
        except Exception as e:
            logging.debug('search/commits parse error: %s', e)
            return
        items = data.get('items') or []
        if not items:
            return
        for item in items:
            commit = item.get('commit') or {}
            repo = (item.get('repository') or {}).get('full_name', '')
            sha = item.get('sha', '')
            date = (commit.get('author') or {}).get('date', '')
            message = commit.get('message') or ''
            for role in ('author', 'committer'):
                who = commit.get(role) or {}
                email = who.get('email')
                name = who.get('name') or ''
                if not email:
                    continue
                key = (email.lower(), name.lower(), role)
                if key in seen:
                    continue
                seen.add(key)
                yield {
                    'email': email, 'name': name, 'role': role,
                    'repo': repo, 'sha': sha, 'date': date,
                }
            # trailers in the commit message body (Signed-off-by, Co-authored-by, ...)
            for tm in TRAILER_RE.finditer(message):
                t_key = tm.group('key').lower()
                t_name = (tm.group('name') or '').strip()
                t_email = (tm.group('email') or '').strip()
                if not t_email:
                    continue
                # reject malformed names: ':' implies another trailer label was
                # crammed onto the same line; '@' implies a @-mention or stray
                # handle. Real personal names don't contain either.
                if ':' in t_name or '@' in t_name:
                    continue
                key = (t_email.lower(), t_name.lower(), t_key)
                if key in seen:
                    continue
                seen.add(key)
                yield {
                    'email': t_email, 'name': t_name, 'role': t_key,
                    'repo': repo, 'sha': sha, 'date': date,
                }
        if len(items) < GITHUB_PER_PAGE_LIMIT:
            return


def print_search_results(results, ignore_noreply=True):
    """Pretty-print search_commits_by_author() output grouped by (email, name)."""
    groups = {}
    for r in results:
        if ignore_noreply and is_system_email(r['email']):
            continue
        key = (r['email'], r['name'])
        groups.setdefault(key, []).append(r)

    if not groups:
        print('No public commits found via /search/commits.')
        return

    print('Found {} unique (email, name) identities:'.format(len(groups)))
    print(DELIMITER)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for (email, name), rows in ordered:
        repos = sorted({r['repo'] for r in rows if r['repo']})
        roles = sorted({r['role'] for r in rows})
        roles_str = ', '.join(roles)
        print('{}  <{}>  x{}  [{}]'.format(
            name or '<no name>', email, len(rows), roles_str
        ))
        for repo in repos[:5]:
            print('    repo: {}'.format(repo))
        if len(repos) > 5:
            print('    ... +{} more repos'.format(len(repos) - 5))


def find_all_repos_recursively(path):
    git_dirs = []
    for current_dir, dirs, _ in os.walk(path):
        if current_dir.endswith('.git'):
            git_dirs.append(current_dir)
            while dirs:
                dirs.pop()

    return git_dirs


class Commit:
    """
        Extract and store basic commit info
    """
    @staticmethod
    def _extract_name_email(log_str_part):
        extracted = re.search(LOG_NAME_REGEXP, log_str_part)
        if not extracted:
            logging.error('Could not extract name/email from "%s"', log_str_part)
            return ('', '')

        return extracted.groups()


    def __init__(self, log_str):
        extracted = re.search(LOG_REGEXP, log_str)
        if not extracted:
            logging.error('Could not commit info from "%s"', log_str)
        else:
            self.hash, self.author, self.committer = extracted.groups()
            self.author_name, self.author_email = Commit._extract_name_email(self.author)
            self.committer_name, self.committer_email = Commit._extract_name_email(self.committer)

            self.author_committer_names_same = self.author_name == self.committer_name
            self.author_committer_emails_same = self.author_email == self.committer_email

            self.author_committer_same = self.author_committer_names_same and self.author_committer_emails_same


    def __str__(self):
        return """Hash: {hash}
Author name: {author_name}
Author email: {author_email}
Committer name: {committer_name}
Committer email: {committer_email}
        """.format(
            hash=self.hash,
            author_name=self.author_name, author_email=self.author_email,
            committer_name=self.committer_name, committer_email=self.committer_email,
        )


class Git:
    """
        Make external git work
    """
    @staticmethod
    def get_tree_info(git_dir):
        process = subprocess.Popen(GIT_EXTRACT_CMD, cwd=git_dir, shell=True, stdout=subprocess.PIPE)
        stat = process.stdout.read().decode()
        return stat

    @staticmethod
    def clone(link):
        process = subprocess.Popen(GIT_CLONE_CMD.format(link), shell=True, stdout=subprocess.PIPE)
        res = process.stdout.read().decode()
        return res

    @staticmethod
    def get_verified_username(repo_url, commit, person):
        if not repo_url.startswith('https://github.com/'):
            return

        commit_link = repo_url.rstrip('/') + '/commit/' + commit.hash
        req = urllib.request.Request(commit_link)
        try:
            response = urllib.request.urlopen(req)
            page_source = response.read()

            # TODO: authored and committed
            extracted = re.search(r'<a href=".+?commits\?author=(.+?)"', str(page_source))
            if not extracted:
                return

            name = extracted.groups(0)[0]
            person.github_link = name
            logging.debug(commit_link + '\n' + name)

        except Exception as e:
            logging.debug(e)


class Person:
    """
        Basic person info from commit
    """
    def __init__(self, desc):
        self.name = ''
        self.email = ''
        self.desc = desc
        self.as_author = 0
        self.as_committer = 0
        self.also_known = {}
        self.github_link = None

    def __str__(self):
        result = "Name:\t\t\t{name}\nEmail:\t\t\t{email}".format(name=self.name, email=self.email)
        if self.as_author:
            result += "\nAppears as author:\t{} times".format(self.as_author)
        if self.as_committer:
            result += "\nAppears as committer:\t{} times".format(self.as_committer)
        if self.github_link:
            result += "\nVerified account:\n\t\t\thttps://github.com/{}".format(self.github_link)
        if self.also_known:
            result += '\nAlso appears with:{}'.format(
                '\n\t\t\t'.join(['']+list(self.also_known.keys()))
            )

        return result


class GitAnalyst:
    """
        Git analysis
    """
    def __init__(self):
        self.git = Git()

        self.commits = []
        self.persons = {}
        self.names = {}
        self.emails = {}
        self.repos = []
        self.same_emails_persons = {}

    def append(self, source=None):
        if not source:
            return

        if not '://' in source:
            git_dir = source
        else:
            self.git.clone(source)
            git_dir = source.split('/')[-1]

        self.repos.append(git_dir)
        git_info = self.git.get_tree_info(git_dir)
        text_commits = filter(lambda x: x, git_info.split('\n'))
        new_commits = list(map(Commit, text_commits))
        self.commits += new_commits

        self.analyze(new_commits, source)

    @property
    def sorted_persons(self):
        return sorted(self.persons.items(), key=lambda p: p[1].as_author + p[1].as_committer)

    def resolve_persons(self):
        threads = []
        for _, person in self.persons.items():
            if person.email in SYSTEM_EMAILS:
                continue
            # TODO: optimize
            thread = Thread(target=self.git.get_verified_username, args=(person.repo_url, person.commit, person))
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

    def analyze(self, new_commits, repo_url):
        # save all author and committers as unique persons
        for commit in new_commits:
            # author saving
            person = self.persons.get(commit.author, Person(commit.author))
            person.name = commit.author_name
            person.email = commit.author_email
            person.as_author += 1
            person.repo_url = repo_url
            person.commit = commit
            self.persons[commit.author] = person

            # committer saving
            person = self.persons.get(commit.committer, Person(commit.committer))
            person.name = commit.committer_name
            person.email = commit.committer_email
            person.as_committer += 1
            person.repo_url = repo_url
            person.commit = commit
            self.persons[commit.committer] = person

        # make persons graph links based on author/committer mismatch
        for commit in new_commits:
            if not commit.author_committer_same:
                self.persons[commit.author].also_known[commit.committer] = self.persons[commit.committer]
                self.persons[commit.committer].also_known[commit.author] = self.persons[commit.author]

        # TODO: probabilistic graph links based on same names/emails and Levenshtein distance
        # just checking same names now

        for commit in new_commits:
            author_emails = self.names.get(commit.author_name, set())
            author_emails.add(commit.author_email)
            self.names[commit.author_name] = author_emails

            committer_emails = self.names.get(commit.committer_name, set())
            committer_emails.add(commit.committer_email)
            self.names[commit.committer_name] = committer_emails

        for emails_set in self.names.values():
            names = [name for name, v in self.names.items() if v == emails_set]
            key = ','.join(sorted(names))
            if len(names) > 1 and key not in self.same_emails_persons:
                self.same_emails_persons[key] = (names, emails_set)

        return self.sorted_persons

    def __str__(self):
        result = 'Analyze of the git repo(s) "{}"'.format(', '.join(self.repos))

        result += '\nVerbose persons info:\n'
        for name, person in self.sorted_persons:
            result += ("{}\n{}\n".format(DELIMITER, person))

        matching_result = ''
        for name, emails in self.names.items():
            if len(emails) > 1:
                matching_result += '\n{} is the owner of emails:\n\t\t\t{}\n'.format(name, '\n\t\t\t'.join(emails))

        if matching_result:
            result += '\nMatching info:\n{}{}'.format(DELIMITER, matching_result)

        for names, emails in self.same_emails_persons.values():
            result += '\n{} are the same person\n'.format(' and '.join(names))

        result += '\nStatistics info:\n{}'.format(DELIMITER)
        result += '\nTotal persons: {}'.format(len(self.persons))

        return result


def main():
    parser = argparse.ArgumentParser(description='Extract accounts\' information from git repo and make some researches.')
    parser.add_argument('-d', '--dir', help='directory with git project(s)')
    parser.add_argument('-u', '--url', help='url of git repo')
    parser.add_argument('--github', action='store_true', help='try to extract extended info from GitHub')
    parser.add_argument('--nickname', type=str, help='try to download repos from all platforms by nickname')
    parser.add_argument('--search', type=str, metavar='USERNAME',
                        help='use /search/commits API to find emails by author (no cloning, ~1000 results max)')
    parser.add_argument('--no-ignore-noreply', action='store_true',
                        help='do not filter @users.noreply.github.com / noreply@github.com from --search results')
    parser.add_argument('-r', '--recursive', action='store_true', help='recursive directory processing')
    parser.add_argument('--debug', action='store_true', help='print debug information')
    # TODO: clone repos as bare
    # TODO: allow forks

    args = parser.parse_args()
    log_level = logging.INFO if not args.debug else logging.DEBUG
    logging.basicConfig(level=log_level, format='-'*40 + '\n%(levelname)s: %(message)s')

    if args.search:
        token = os.environ.get('GITHUB_TOKEN')
        ignore = not args.no_ignore_noreply
        gpg = list(get_gpg_keys_emails(args.search, token=token))
        had_gpg = print_gpg_results(gpg, ignore_noreply=ignore)
        results = list(search_commits_by_author(args.search, token=token))
        print_search_results(results, ignore_noreply=ignore)
        if not had_gpg and not results:
            print('No emails found via /gpg_keys or /search/commits.')
        return

    analyst = None

    analyst = GitAnalyst()
    repos = []

    repos.append(args.url)
    repos.append(args.dir and args.dir.rstrip('/'))

    if args.recursive and args.dir:
        dirs = find_all_repos_recursively(args.dir)
        repos += dirs

    if args.nickname:
        repos_count = get_public_repos_count(args.nickname)
        if repos_count:
            print('found', repos_count, 'repos')
            repos += get_github_repos(args.nickname, repos_count=repos_count)

    for repo in repos:
        analyst.append(source=repo)

    logging.info('Resolving GitHub usernames, please wait...')
    analyst.resolve_persons()

    if analyst.repos:
        print(analyst)
    else:
        print('Run me with git repo link or path!')


if __name__ == '__main__':
    main()
