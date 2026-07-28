# mdc-instagram-pipeline

Automated Instagram content pipeline for [maisondecocon.com](https://maisondecocon.com)
(Shopify). Drafts an Instagram companion post whenever a new blog post
publishes, holds it for manual approval, and publishes it only once a human
runs a second workflow. Mirrors the architecture of the sibling
[`mdc-blog-pipeline`](https://github.com/dstark-maison/mdc-blog-pipeline) repo
(secret naming, Resend sender setup, Shopify auth pattern, commit conventions)
where relevant.

## Files

- `ig_draft.py` — draft stage (`.github/workflows/ig-draft.yml`, runs on a schedule)
- `ig_publish.py` — publish stage (`.github/workflows/ig-publish.yml`, run manually, once per approved post)
- `shopify_client.py` — shared Shopify Admin API auth (client_credentials grant — see below)
- `ig_publish_test.py` — standalone Graph API smoke test used to validate the
  publish flow and record Meta's required App Review screencast. Not part of
  the automated pipeline; kept for reference.
- `state/ig-drafted-articles.json` — tracks which articles have already been drafted (committed back automatically)
- `generated/` — cropped square images, committed here so they have a public `raw.githubusercontent.com` URL

## How the flow works, end to end

1. Twice a day, `ig-draft.yml` runs automatically. It checks Shopify for the
   most recent blog articles and filters out ones already drafted.
2. For each new, published post, it drafts a caption via Claude, crops the
   post's featured image to a square, creates an unpublished Instagram media
   container, and emails an approval request via Resend.
3. You review the email. If you want to publish it: go to the **Actions**
   tab on GitHub → **IG Publish** workflow → **Run workflow** → paste in the
   `creation_id` and `blog_post_url` values from the email → click the green
   **Run workflow** button.
4. That triggers `ig-publish.yml`, which publishes the post to Instagram and
   updates a Shopify redirect so your Instagram bio link points to that post.
5. If you don't want to publish a drafted post, just don't run the workflow.
   Nothing happens automatically — the container simply sits unpublished.

## One-time manual setup required

### 1. Set your Instagram bio link once

In the Instagram app, set your bio's website link to a fixed URL on your
own domain, e.g.:

```
https://www.maisondecocon.com/pages/latest-post
```

This path doesn't need to be a real page — it just needs to exist as a
**redirect source**. You'll create the actual redirect once manually (or
let the first pipeline run create it automatically — `ig_publish.py`
creates the redirect if it doesn't already exist).

**Important:** whatever path you choose here must exactly match the
`BIO_LINK_PATH` secret below (just the path, e.g. `/pages/latest-post`,
not the full URL).

### 2. Shopify app scope

This pipeline reuses `mdc-blog-pipeline`'s existing Shopify custom app
credentials (client_credentials grant — this store's app is a Dev Dashboard
app with no static Admin API token). That app currently has `read_content`
(reads blogs/articles) but has **not** been granted **`write_online_store_navigation`**,
which is the scope `urlRedirectCreate`/`urlRedirectUpdate` actually require
(confirmed against Shopify's own mutation docs — not `write_content`, which
was the original guess). Add that scope to the app in the Shopify admin
before testing `ig-publish.yml`.

### 3. GitHub Actions secrets to add

Go to the repo → Settings → Secrets and variables → Actions, and add:

| Secret | Value |
|---|---|
| `SHOPIFY_STORE_DOMAIN` | `zpjzx0-gy.myshopify.com` (confirmed — this is the store's real `*.myshopify.com` backend domain, distinct from the storefront domain) |
| `SHOPIFY_CLIENT_ID` | Same custom app credential used by `mdc-blog-pipeline` |
| `SHOPIFY_CLIENT_SECRET` | Same custom app credential used by `mdc-blog-pipeline` |
| `SHOPIFY_BLOG_HANDLE` | `blog` (confirmed via live Admin API query) |
| `STOREFRONT_DOMAIN` | `www.maisondecocon.com` (confirmed against `mdc-blog-pipeline`'s hardcoded convention) |
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `META_ACCESS_TOKEN` | the System User access token (from the "IG Publish Pipeline" System User — this is the Page-scoped token, not the raw System User token) |
| `IG_USER_ID` | `17841473943836366` |
| `RESEND_API_KEY` | your Resend API key |
| `RESEND_FROM_IG` | `MDC Instagram Pipeline <ig-pipeline@reports.maisondecocon.com>` — matches `mdc-blog-pipeline`'s display-name format; `reports.maisondecocon.com` is already verified in Resend |
| `RESEND_TO` | `ds@maisondecocon.com` (same inbox `mdc-blog-pipeline` reports to) |
| `BIO_LINK_PATH` | `/pages/latest-post` (or whatever you chose in step 1) |

### 4. Optional: brand voice file

If you place a `brand-voice.md` file in the repo root, `ig_draft.py` will
automatically read it and include it in the Claude prompt for tone
guidance. Not required — the script works without it, just with generic
brand-voice instructions built in.

## Corrections made after checking against real infrastructure

The original draft of this pipeline was built without direct access to the
store/repos and guessed at several things. After checking the live Shopify
Admin API and the `mdc-blog-pipeline` repo, here's what changed:

- **Shopify auth was a fundamentally wrong shape.** The original code assumed
  a static `SHOPIFY_ADMIN_TOKEN`. This store's app is a Dev Dashboard app
  with no static token — auth is `client_credentials` grant via
  `SHOPIFY_CLIENT_ID`/`SHOPIFY_CLIENT_SECRET`, fetched fresh per run
  (see `shopify_client.py`, mirroring `mdc-blog-pipeline/src/lib/shopify.js`).
- **`SHOPIFY_STORE_DOMAIN` placeholder was wrong** — real value is
  `zpjzx0-gy.myshopify.com`, not `maison-de-cocon.myshopify.com`.
- **Blog handle confirmed**: `blog` (there's only one blog on this store).
- **Several real bugs in the GraphQL query, all confirmed against the live
  schema/docs before fixing (not guessed):**
  - `blogByHandle` doesn't exist on `QueryRoot` at all — only `blog(id:)` and
    `blogs(query:, ...)`. First real run of `ig-draft.yml` failed on this
    (`Field 'blogByHandle' doesn't exist on type 'QueryRoot'`) because this
    field name was never actually re-verified when the sortKey/excerpt fixes
    below were made — only the two now fixed. Now uses
    `blogs(first: 1, query: "handle:<handle>")`.
  - `articles(..., sortKey: PUBLISHED_AT, ...)` — `sortKey` isn't a valid
    argument on `Blog.articles` at all.
  - `excerpt` was requested on `Article`, which doesn't exist (the real
    field is `summary`).
  - `ig_publish.py`'s redirect lookup passed the raw path as the `query`
    filter with no field prefix, which falls back to an unreliable
    full-text search across multiple fields instead of matching `path`
    specifically. Shopify's search syntax needs an explicit `path:` prefix
    (e.g. `path:/pages/latest-post`) — fixed.
- **Redirect write scope was wrong**: needs `write_online_store_navigation`,
  not `write_content` (see setup step 2 above).
- **API version** (`2025-01`) was correct as originally guessed — matches
  `mdc-blog-pipeline`'s convention exactly.
- **Storefront domain** (`www.maisondecocon.com`) was correct as originally
  guessed — matches `mdc-blog-pipeline`'s hardcoded convention exactly.
- Added an `isPublished` filter so an unpublished/scheduled article
  can never get drafted into an Instagram post pointing at a dead link.
- Added a container-status poll (`status_code == FINISHED`) before
  publishing in `ig_publish.py`, mirroring the tested working flow in
  `ig_publish_test.py` — cheap insurance in case a post gets approved
  within seconds of being drafted.
- Removed the documented-but-unused `LOOKBACK_HOURS` env var — dedup is
  already handled by `state/ig-drafted-articles.json`, so it did nothing.
- `CLAUDE_MODEL` default updated to `claude-sonnet-5` (current model —
  matches `mdc-blog-pipeline`'s production workflow).
- `generate_caption()` assumed `content[0]` was always the text block.
  `claude-sonnet-5` returns a leading `thinking` block by default (confirmed
  live — first real run failed with `KeyError: 'text'`), so it now finds the
  first block with `type == "text"` instead of assuming position 0.
- **Image commit+push was happening too late.** The original design created
  the Instagram media container using the image's `raw.githubusercontent.com`
  URL immediately, but only committed + pushed that image in a later,
  separate workflow step — so Meta's servers were asked to fetch a file that
  didn't exist yet (confirmed live: `400 "The media could not be fetched
  from this URI"`). `git_commit_and_push()` in `ig_draft.py` now commits +
  pushes the image (and then the state file) mid-run, immediately after each
  is written, before the next step needs it live. The workflow's old,
  separate "commit generated image + state" step was removed since the
  script now owns this entirely.

## ⚠️ Open blocker: this repo is private, and that's incompatible with `raw.githubusercontent.com`

Confirmed directly (anonymous `curl` to an already-committed, already-pushed
image URL returns `404`): `raw.githubusercontent.com` does not serve files
from a private repo to unauthenticated requests, and Meta's Graph API fetches
image URLs anonymously. This is **not a timing issue** (that one's fixed
above) — it will 404 forever, for every image, regardless of ordering, as
long as this repo stays private. This blocks `ig-draft.yml` from working at
all until resolved. Options, needing a decision:

1. **Make this repo public.** Nothing in it is actually sensitive — no
   secrets live in the repo (those are GitHub Actions secrets), and the
   committed images are just featured images from already-public blog posts.
   Smallest change, keeps the "no new infrastructure" approach intact.
2. **Move image hosting elsewhere** (e.g. Shopify's own file storage, as
   originally flagged as a future fallback) — bigger change, and the kind of
   thing explicitly flagged as "discuss before replacing."

Not decided yet — do not merge past this point without picking one.

## Open items (intentionally not solved here)

- **Hashtag strategy**: currently just asks Claude to generate 8–15
  "relevant" hashtags with no dedicated research process — intentional for
  this pass.

## Not yet tested end-to-end

This has been built but not run against the real Shopify store or a real
scheduled trigger yet. Recommended first test:

1. Add all secrets above (and the `write_online_store_navigation` scope)
2. Manually trigger `ig-draft.yml` via workflow_dispatch (not waiting for
   the schedule)
3. Confirm the approval email arrives and looks right
4. Manually trigger `ig-publish.yml` with the values from that email
5. Confirm the post goes live and the bio-link redirect updates correctly
