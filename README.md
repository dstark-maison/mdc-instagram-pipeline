# MDC social pipeline (`dstark-maison/mdc-social-pipeline`, formerly `mdc-instagram-pipeline`)

Blog post → social copy → **one Resend approval email** → publish → verified in a ledger.
Phase 1 covers Instagram. Facebook, Pinterest and Reddit follow the same three steps.

## Flow

1. **Draft** (`social-draft.yml`, twice daily, `draft.py`): for each published article
   without a ledger entry, Claude writes the copy, the featured image is cropped to 1:1 and
   committed under `generated/` (public raw URL), the draft is saved to
   `state/drafts/{article_id}/ig.json`, and one email goes out. **Nothing is created on Instagram.**
2. **Approve**: the email has *Approve* / *Reject* buttons. Each opens a prefilled GitHub issue
   (label `approval`, title `approve:{article_id}:ig`). Click **Submit new issue**.
3. **Publish** (`social-publish.yml`, `publish.py`): creates the IG container, publishes, reads
   the post back by id (permalink + caption must match), writes `state/ledger.json`, repoints the
   bio redirect and reads that back too. The result is commented on the issue and the issue is closed.
   *Reject* marks the draft `rejected` and publishes nothing.

Any error → failure email with the platform's raw error text. A step is never marked done
without its read-back.

## Ledger (`state/ledger.json`)

`{article_id: {channel: {status, handle, post_id, url, timestamp}}}` — the key is the **numeric Shopify article id** (handles get renamed; ids do not). The handle is stored for display only.

| status | meaning |
|---|---|
| `draft` | copy saved, nothing on the platform |
| `publishing` | publish was called but not verified; **blocks automatic retries** (could double-post). The failure email contains the exact entry to paste for both outcomes (post live / not live) |
| `published` | posted and read back; re-running is a no-op |
| `rejected` | rejected at the approval step |

## Drafts stay approvable indefinitely

- A draft is keyed by numeric article id. A newer cron/manual run never overwrites, expires or rejects the
  draft of a *different* article, and the Instagram container is created only at publish time (no 24 h expiry).
- Re-drafting the *same* article replaces its draft content; the existing approval link (same id) publishes the latest version.
- `/pages/latest-post` always points to the article of the **most recent successful IG publish** (by publish time):
  every successful publish repoints it, including an older article approved later. The pointer is kept in the ledger
  under `_bio_link`; it is only refused to a run whose publish time is older than the current holder's (two simultaneous
  approvals), never by article date.
- Every draft email ends with **Pending approvals**: all other drafts still in `draft` status, each with its Approve link.
- Image URLs are never stored; they are built from the current repo name when an email is sent or a post is published.

## Meta token

Secret `META_SYSTEM_USER_TOKEN` (system user, never expires). At the start of every draft and publish run
`core/meta.py` calls `debug_token`; an invalid token or a missing scope stops the run and the failure email names it.
Required now: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
(`pages_manage_posts` is added with Phase 2). *Actions → Social Token Check* runs the same check by hand.
Tokens are sent as `Authorization` headers and redacted from every log, email and issue comment.

## Which articles get drafted

- **Cron** (`social-draft.yml`): only articles *published after* `DRAFT_CUTOFF` (`core/config.py`, set to the
  merge time) with no ledger entry. Older articles are never drafted automatically.
- **On request**: *Actions → Social Draft → Run workflow* with `article_id` (numeric). Works for any age, but
  refuses held articles and anything already `published` / `publishing`.
- **Hold list** (`HOLD_ARTICLE_IDS` in `core/config.py`): articles blocked for both paths until their claims are
  fixed. Remove the entry to release one.
- `state/legacy-ig-drafted-articles.json` is read-only history of the old pipeline; nothing reads it.
- Legacy posts that were live on Instagram were seeded into the ledger as `published` (`seeded_from` says how)
  by `tools/ig_media_audit.py`, a read-only image-hash match of the account's media against `generated/`.

## Approval security

- Only issues with the `approval` label from an `OWNER` or `COLLABORATOR` author run anything.
  Add the account you are signed in as (e.g. `drostark`) as a collaborator.
- The title is passed via `env:` and must fully match
  `^(approve|reject):[0-9]+:(ig|fb|ig\+fb|pin|reddit)$` (`core/approval.py: ACTION_RE`).
  Anything else fails. It is never interpolated into a shell line.

## Manual run / failure test

*Actions → Social Publish → Run workflow*: `article` (numeric id), `channel`. Tick **test_bad_token** to run with an
invalid Meta token and confirm the failure email arrives (the ledger stays `draft`).

## Layout

```
core/       blog, copy, image, approval (email + buttons), ledger (state + git), http (error bodies, redaction)
channels/   instagram.py   (facebook / pinterest / reddit in later phases)
draft.py    publish.py
tests/      python -m unittest discover -s tests -t .
```

`ig_publish_test.py` is the standalone Graph API smoke test used for Meta App Review; not part of the pipeline.

## Setup

Secrets (repo → Settings → Secrets → Actions): `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_CLIENT_ID`,
`SHOPIFY_CLIENT_SECRET`, `SHOPIFY_BLOG_HANDLE`, `STOREFRONT_DOMAIN`, `ANTHROPIC_API_KEY`,
`META_SYSTEM_USER_TOKEN`, `IG_USER_ID`, `RESEND_API_KEY`, `RESEND_FROM_IG`, `RESEND_TO`, `BIO_LINK_PATH`.
(Names unchanged from the old pipeline.) The Shopify app needs `read_content` and
`write_online_store_navigation`. Instagram bio link: set once, by hand, to `https://www.maisondecocon.com{BIO_LINK_PATH}`.
Optional `brand-voice.md` in the repo root is added to the copy prompt.

The repo is public: drafts and the ledger are readable before approval, by design.
Reddit inputs (Search Console data, findquestions lists) must never be committed (`reddit/inbox/` is ignored).
