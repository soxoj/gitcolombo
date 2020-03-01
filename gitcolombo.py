#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import subprocess
import urllib.request


DELIMITER = '---------------'

LOG_FORMAT = r'%H;"%an %ae";"%cn %ce"'
LOG_REGEXP = r'(\w+);"(.*?)";"(.*?)"'
LOG_NAME_REGEXP = r'^(.+?)\s+(\S+)$'

GIT_EXTRACT_CMD = "git log --pretty='{}' --all".format(LOG_FORMAT)
GIT_CLONE_CMD = "git clone {}"

GITHUB_USER_REPOS = 'https://api.github.com/users/{}/repos'
GITHUB_ORGS_REPOS = 'https://api.github.com/orgs/{}/repos'

# TODO: "system" git accounts marks (e.g. noreply@github.com)


def get_github_repos(nickname, only_forks=True):
    repos_links = set()
    for url in [GITHUB_ORGS_REPOS, GITHUB_USER_REPOS]:
        req_url = url.format(nickname)
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
            logging.error('Could not extract info from %s', log_str_part)
            return ('', '')

        return extracted.groups()


    def __init__(self, log_str):
        extracted = re.search(LOG_REGEXP, log_str)
        if not extracted:
            logging.error('Could not extract info from %s', log_str)
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

    def __str__(self):
        result = "Name:\t\t\t{name}\nEmail:\t\t\t{email}".format(name=self.name, email=self.email)
        if self.as_author:
            result += "\nAppears as author:\t{} times".format(self.as_author)
        if self.as_committer:
            result += "\nAppears as committer:\t{} times".format(self.as_committer)
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

        self.persons = {}
        self.names = {}
        self.emails = {}
        self.repos = []

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
        self.commits = list(map(Commit, text_commits))

        self.analyze()

    @property
    def sorted_persons(self):
        return sorted(self.persons.items(), key=lambda p: p[1].as_author + p[1].as_committer)

    def analyze(self):
        # save all author and committers as unique persons
        for commit in self.commits:
            person = self.persons.get(commit.author, Person(commit.author))
            person.name = commit.author_name
            person.email = commit.author_email
            person.as_author += 1
            self.persons[commit.author] = person

            person = self.persons.get(commit.committer, Person(commit.committer))
            person.name = commit.committer_name
            person.email = commit.committer_email
            person.as_committer += 1
            self.persons[commit.committer] = person

        # make persons graph links based on author/committer mismatch
        for commit in self.commits:
            if not commit.author_committer_same:
                self.persons[commit.author].also_known[commit.committer] = self.persons[commit.committer]
                self.persons[commit.committer].also_known[commit.author] = self.persons[commit.author]

        # TODO: probabilistic graph links based on same names/emails and Levenshtein distance
        # just checking same names now

        for commit in self.commits:
            author_emails = self.names.get(commit.author_name, set())
            author_emails.add(commit.author_email)
            self.names[commit.author_name] = author_emails

            committer_emails = self.names.get(commit.committer_name, set())
            committer_emails.add(commit.committer_email)
            self.names[commit.committer_name] = committer_emails

        self.same_emails_persons = {}
        for emails_set in self.names.values():
            names = [name for name, v in self.names.items() if v == emails_set]
            key = ','.join(sorted(names))
            if len(names) > 1 and not key in self.same_emails_persons:
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
                matching_result += '\n{} pretends to own emails:\n\t\t\t{}\n'.format(name, '\n\t\t\t'.join(emails))

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
    parser.add_argument('-r', '--recursive', action='store_true', help='recursive directory processing')
    parser.add_argument('--debug', action='store_true', help='print debug information')
    # TODO: clone repos as bare
    # TODO: allow forks

    args = parser.parse_args()
    log_level = logging.INFO if not args.debug else logging.DEBUG

    analyst = None

    analyst = GitAnalyst()
    repos = []

    repos.append(args.url)
    repos.append(args.dir and args.dir.rstrip('/'))

    if args.recursive and args.dir:
        dirs = find_all_repos_recursively(args.dir)
        repos += dirs

    if args.nickname:
        repos += get_github_repos(args.nickname)

    for repo in repos:
        analyst.append(source=repo)

    if analyst.repos:
        print(analyst)
    else:
        print('Run me with git repo link or path!')


if __name__ == '__main__':
    main()
