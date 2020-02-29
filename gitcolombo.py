#!/usr/bin/env python3
import argparse
import logging
import re
import subprocess


DELIMITER = '---------------'

LOG_FORMAT = r'%H;"%an %ae";"%cn %ce"'
LOG_REGEXP = r'(\w+);"(.*?)";"(.*?)"'
LOG_NAME_REGEXP = r'^(.+?)\s+(\S+)$'

GIT_EXTRACT_CMD = "git log --pretty='{}' --all".format(LOG_FORMAT)
GIT_CLONE_CMD = "git clone {}"

# TODO: "system" git accounts marks (e.g. noreply@github.com)


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
    def __init__(self, git_dir=None, url=None):
        git = Git()
        if url:
            git.clone(url)
            git_dir = url.split('/')[-1]

        self.repo_name = git_dir
        git_info = git.get_tree_info(git_dir)
        text_commits = filter(lambda x: x, git_info.split('\n'))
        self.commits = list(map(Commit, text_commits))

        self.persons = {}
        self.names = {}
        self.emails = {}

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

        return self.sorted_persons

    def __str__(self):
        result = 'Analyze of the git repo "{}"'.format(self.repo_name)

        result += '\nVerbose persons info:\n'
        for name, person in self.sorted_persons:
            result += ("{}\n{}\n".format(DELIMITER, person))

        matching_result = ''
        for name, emails in self.names.items():
            if len(emails) > 1:
                matching_result += '\n{} pretends to own emails:\n\t\t\t{}\n'.format(name, '\n\t\t\t'.join(emails))

        if matching_result:
            result += '\nMatching info:\n{}{}'.format(DELIMITER, matching_result)

        result += '\nStatistics info:\n{}'.format(DELIMITER)
        result += '\nTotal persons: {}'.format(len(self.persons))

        return result


def main():
    parser = argparse.ArgumentParser(description='Extract accounts\' information from git repo and make some researches.')
    parser.add_argument('-d', '--dir', help='directory with git project(s)')
    parser.add_argument('-u', '--url', help='url of git repo')
    parser.add_argument('--github', action='store_true', help='try to extract extended info from GitHub')
    parser.add_argument('--debug', action='store_true', help='print debug information')

    args = parser.parse_args()
    log_level = logging.INFO if not args.debug else logging.DEBUG

    analyst = None

    if args.dir:
        analyst = GitAnalyst(git_dir=args.dir)
    elif args.url:
        analyst = GitAnalyst(url=args.url)
    else:
        print('Run me with git repo link or path!')

    if analyst:
        print(analyst)


if __name__ == '__main__':
    main()
