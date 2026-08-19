# Repo hooks

This repo is **public by choice** — the code is meant to be readable, the life log
behind it is not. `pre-commit` enforces that line on staged changes.

```sh
git config core.hooksPath .githooks     # once per clone
```

It blocks four things: files that should never be committed (`.env`, `*.db`, keys),
credential-shaped strings (placeholders and `$VAR` references pass), real-world
identifiers (stranger emails, absolute home paths), and anything matching a
personal denylist.

**The denylist is not in this repo.** It lives at `.git/private-denylist` — one
case-insensitive regex per line — because a tracked list of the names you're trying to
keep out would commit the very strings it's meant to block. `.git/` is never pushed, so
the list can name names. Add to it as new people or hostnames enter your life.

Deliberately *not* blocked: the repo owner's own name and email. They're already public
as the account that owns this repo, so listing them would only create false positives.

Bypass for a genuine false positive: `git commit --no-verify`.
