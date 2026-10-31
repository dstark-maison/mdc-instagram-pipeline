# MDC social pipeline

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

`state/legacy-ig-drafted-articles.json` lists articles handled by the old pipeline; they are skipped.

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
`META_ACCESS_TOKEN`, `IG_USER_ID`, `RESEND_API_KEY`, `RESEND_FROM_IG`, `RESEND_TO`, `BIO_LINK_PATH`.
(Names unchanged from the old pipeline.) The Shopify app needs `read_content` and
`write_online_store_navigation`. Instagram bio link: set once, by hand, to `https://www.maisondecocon.com{BIO_LINK_PATH}`.
Optional `brand-voice.md` in the repo root is added to the copy prompt.

The repo is public: drafts and the ledger are readable before approval, by design.
Reddit inputs (Search Console data, findquestions lists) must never be committed (`reddit/inbox/` is ignored).
