# mdc-social-pipeline (formerly mdc-instagram-pipeline)

## Session start: repo sync check

At the start of every session, run the repo sync check script from the theme repo's `tools/` folder (`check-repos.ps1`).
If any repo reports BEHIND, `git pull --ff-only` it before doing other work. Report anything ahead, untracked, modified, or
not on its default branch to the user rather than fixing it silently.
