# mdc-publish-content-meta

## Session start: repo sync check

At the start of every session, run `powershell -File C:\002_Projects\250118_MaisonDeCocon\maison-de-cocon-shopify-theme\tools\check-repos.ps1` (in the theme repo).
If any repo reports BEHIND, `git pull --ff-only` it before doing other work. Report anything ahead, untracked, modified, or
not on its default branch to the user rather than fixing it silently.
