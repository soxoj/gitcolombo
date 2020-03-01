# Gitcolombo

![Logo](https://avatars.mds.yandex.net/get-zen_doc/99101/pub_5d641327027a1500ad1d8cc2_5d6413cdf73d9d00adeba784/scale_600)

### Description

OSINT tool to extract info about persons from git repositories: common names, emails, matches between different (as it may seems) accounts.

### Using

1. **Install git**

2. Run:

        # from any git url
        ./gitcolombo.py -u https://github.com/Kalanchyovskaia16/newlps

        # from directory, recursively
        ./gitcolombo.py -d ./newlps

        # from all GitHub personal/org repos by nickname
        ./gitcolombo.py --nickname LubyRuffy

Output:

- verbose persons info
  - name
  - email
  - number of appearences as author/committer 
  - other persons that person can be

- emails used for the same name
- different names for the same person
- general statistics

### What's the difference between git author and committer?

TL;DR

- author wrote the code (make the patch)
- commiter commit it to the repo (rewrite history, make pull/merge requests...)

Nice explanation: https://stackoverflow.com/questions/18750808/difference-between-author-and-committer-in-git

Very often developers make inaccurate commits with the one name/email (e.g. work account), then change to the right (e.g. personal account) and make `git commit --amend`, but forget to change the author of the commit.
This way we can use it for OSINT as match of names/emails from git history.

### TODO

- [x] Total statistics for repos in a directory
- [ ] Check different names for every email
- [x] GitHub support: clone all repos from account/group
- [ ] GitHub support: extract links to accounts from commit info
- [ ] Exclude "system" accounts (e.g. noreply@github.com)
- [ ] Probabilistic graph links based on same names/emails and Levenshtein distance
- [ ] Other popular git platforms: Gitlab, Stash and also
