# mdc-instagram-pipeline

Automated Instagram content pipeline for [maisondecocon.com](https://maisondecocon.com)
(Shopify). Drafts an Instagram companion post whenever a new blog post
publishes, holds it for manual approval, and publishes it only once a human
runs a second workflow.

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

The Shopify custom app (client_credentials grant, no static Admin API token) needs `read_content` to read articles and `write_online_store_navigation` to create/update the bio-link redirect (`urlRedirectCreate`/`urlRedirectUpdate`).

### 3. GitHub Actions secrets to add

Go to the repo → Settings → Secrets and variables → Actions, and add:

| Secret | Value |
|---|---|
| `SHOPIFY_STORE_DOMAIN` | `your-store.myshopify.com` (the store's `*.myshopify.com` backend domain, distinct from the storefront domain) |
| `SHOPIFY_CLIENT_ID` | Shopify custom app credential |
| `SHOPIFY_CLIENT_SECRET` | Shopify custom app credential |
| `SHOPIFY_BLOG_HANDLE` | `blog` |
| `STOREFRONT_DOMAIN` | `www.maisondecocon.com` |
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `META_ACCESS_TOKEN` | the System User access token (a Page-scoped System User token) |
| `IG_USER_ID` | `your-instagram-business-account-id` |
| `RESEND_API_KEY` | your Resend API key |
| `RESEND_FROM_IG` | `MDC Instagram Pipeline <you@example.com>` (a sender on a domain verified in Resend) |
| `RESEND_TO` | `you@example.com` |
| `BIO_LINK_PATH` | `/pages/latest-post` (or whatever you chose in step 1) |

### 4. Optional: brand voice file

If you place a `brand-voice.md` file in the repo root, `ig_draft.py` will
automatically read it and include it in the Claude prompt for tone
guidance. Not required — the script works without it, just with generic
brand-voice instructions built in.
