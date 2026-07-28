"""
Maison de Cocon — Instagram draft stage.

Runs on a schedule (see ig-draft.yml). For each Shopify blog article
published since the last run:
  1. Generate a caption + hashtags with Claude, using the blog post content
     and (optionally) a local brand-voice.md file for tone/style guidance.
  2. Download the article's featured image and crop it to a 1:1 square.
  3. Commit the cropped image to this repo (generated/) so it has a public
     raw.githubusercontent.com URL — required by the Graph API, and avoids
     needing any new image-hosting infrastructure.
  4. Create an Instagram media container (NOT published yet).
  5. Email an approval request via Resend, with the creation_id and blog
     post URL needed to run the publish workflow.
  6. Record the article as "drafted" in state/ig-drafted-articles.json so
     it's never drafted twice.

This script does not publish anything. Publishing only happens via
ig_publish.py, triggered manually through the ig-publish.yml workflow
after a human reviews the Resend email.

Required environment variables (set as GitHub Actions secrets):
  SHOPIFY_STORE_DOMAIN     e.g. "zpjzx0-gy.myshopify.com" (the *.myshopify.com
                           backend domain, not the storefront domain)
  SHOPIFY_CLIENT_ID        Shopify custom app credentials (client_credentials
  SHOPIFY_CLIENT_SECRET    grant — see shopify_client.py)
  SHOPIFY_BLOG_HANDLE      e.g. "blog" — the blog's handle
  ANTHROPIC_API_KEY        for caption generation
  ACCESS_TOKEN             Meta System User / Page-scoped access token
  IG_USER_ID               Instagram Business Account ID
  RESEND_API_KEY
  RESEND_FROM              e.g. "MDC Instagram Pipeline <ig-pipeline@reports.maisondecocon.com>"
  RESEND_TO                your notification email address
  GITHUB_REPOSITORY        auto-set by GitHub Actions (owner/repo)
  GITHUB_REF_NAME          auto-set by GitHub Actions (branch name)

Optional:
  CLAUDE_MODEL             defaults to "claude-sonnet-5"
"""

import io
import json
import os
import re
import sys
from pathlib import Path

import requests
from PIL import Image

import shopify_client

GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

STATE_PATH = Path("state/ig-drafted-articles.json")
GENERATED_DIR = Path("generated")
BRAND_VOICE_PATH = Path("brand-voice.md")

REQUIRED_ENV = [
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_BLOG_HANDLE", "ANTHROPIC_API_KEY", "ACCESS_TOKEN", "IG_USER_ID",
    "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
    "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
]


def require_env():
    missing = [n for n in REQUIRED_ENV if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"drafted_article_ids": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def fetch_recent_articles(blog_handle, limit=10):
    """Query Shopify Admin GraphQL for the most recent blog articles.

    No sortKey argument: Blog.articles doesn't accept one (confirmed against
    the live Admin API schema) — reverse:true on the default (ID) order is
    the only sort control available, which matches publish order for a blog
    that only ever publishes forward in time.
    """
    query = """
    query RecentArticles($blogHandle: String!, $first: Int!) {
      blogByHandle(handle: $blogHandle) {
        articles(first: $first, reverse: true) {
          nodes {
            id
            title
            handle
            publishedAt
            isPublished
            summary
            body
            image { url }
          }
        }
      }
    }
    """
    data = shopify_client.graphql(query, {"blogHandle": blog_handle, "first": limit})
    blog = data.get("blogByHandle")
    if not blog:
        raise RuntimeError(f"Blog handle '{blog_handle}' not found or no access")
    return blog["articles"]["nodes"]


def build_article_url(store_domain, blog_handle, article_handle):
    # Public storefront domain, not the *.myshopify.com admin domain.
    # If your storefront domain differs from SHOPIFY_STORE_DOMAIN, override
    # this with a STOREFRONT_DOMAIN env var instead.
    storefront = os.environ.get("STOREFRONT_DOMAIN", "www.maisondecocon.com")
    return f"https://{storefront}/blogs/{blog_handle}/{article_handle}"


def generate_caption(article, model):
    """Call Claude to draft a caption + hashtags from the blog article."""
    brand_voice = ""
    if BRAND_VOICE_PATH.exists():
        brand_voice = BRAND_VOICE_PATH.read_text()

    system_prompt = f"""You write Instagram captions for Maison de Cocon, a
quiet-luxury German sleep and bedding brand. Voice: restrained, editorial,
premium — never hype-y, never emoji-stuffed. Tagline: "Sleep, reconsidered."
/ German: "Schlaf, neu gedacht."

{('Brand voice guide:' + brand_voice) if brand_voice else ''}

You will be given a blog post's title and content. Write a companion
Instagram caption that teases the post and drives readers to read more via
the bio link (captions cannot contain clickable links). End with a plain
"Link in bio" call to action (in German, matching the brand's primary
market) — do not include the URL itself in the caption.

Respond ONLY with valid JSON, no markdown fences, no preamble:
{{"caption": "...", "hashtags": ["...", "..."]}}

Caption must be under 2200 characters. Provide 8-15 relevant hashtags,
without the # symbol (added later), mixing brand/niche/broad tags."""

    user_prompt = f"Title: {article['title']}\n\nContent:\n{article.get('summary') or article.get('body', '')[:3000]}"

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    return json.loads(text)


def crop_image_to_square(image_url, output_path):
    """Download the article's featured image and center-crop to 1:1."""
    resp = requests.get(image_url)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    cropped = img.crop((left, top, left + side, top + side))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, "JPEG", quality=90)


def public_raw_url(repo_path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["GITHUB_REF_NAME"]
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{repo_path}"


def create_media_container(ig_user_id, access_token, image_url, caption):
    resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": access_token},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def send_approval_email(article, article_url, caption, hashtags, image_url, creation_id):
    full_caption = caption + "\n\n" + " ".join(f"#{h}" for h in hashtags)
    repo = os.environ["GITHUB_REPOSITORY"]

    html = f"""
    <h2>Review + Publish: {article['title']}</h2>
    <p><a href="{article_url}">{article_url}</a></p>
    <p><img src="{image_url}" style="max-width:400px" /></p>
    <p style="white-space:pre-wrap">{full_caption}</p>
    <hr>
    <p><strong>To publish:</strong></p>
    <ol>
      <li>Go to <a href="https://github.com/{repo}/actions/workflows/ig-publish.yml">the IG Publish workflow</a></li>
      <li>Click "Run workflow"</li>
      <li>Paste these two values into the inputs:
        <ul>
          <li><code>creation_id</code>: <code>{creation_id}</code></li>
          <li><code>blog_post_url</code>: <code>{article_url}</code></li>
        </ul>
      </li>
      <li>Click the green "Run workflow" button</li>
    </ol>
    <p>To skip this post, just don't run the workflow — nothing publishes on its own.</p>
    """

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "from": os.environ["RESEND_FROM"],
            "to": [os.environ["RESEND_TO"]],
            "subject": f"Review + Publish (Instagram): {article['title']}",
            "html": html,
        },
    )
    resp.raise_for_status()


def main():
    require_env()
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

    state = load_state()
    drafted_ids = set(state["drafted_article_ids"])

    articles = fetch_recent_articles(os.environ["SHOPIFY_BLOG_HANDLE"])

    new_articles = [
        a for a in articles if a["id"] not in drafted_ids and a["isPublished"]
    ]

    if not new_articles:
        print("No new articles to draft.")
        return

    for article in new_articles:
        print(f"Drafting Instagram post for: {article['title']}")

        article_url = build_article_url(
            os.environ["SHOPIFY_STORE_DOMAIN"],
            os.environ["SHOPIFY_BLOG_HANDLE"],
            article["handle"],
        )

        content = generate_caption(article, model)

        image_repo_path = f"generated/ig-{article['handle']}.jpg"
        crop_image_to_square(article["image"]["url"], Path(image_repo_path))
        image_url = public_raw_url(image_repo_path)

        creation_id = create_media_container(
            os.environ["IG_USER_ID"], os.environ["ACCESS_TOKEN"],
            image_url, content["caption"] + "\n\n" + " ".join(f"#{h}" for h in content["hashtags"]),
        )

        send_approval_email(
            article, article_url, content["caption"], content["hashtags"],
            image_url, creation_id,
        )

        drafted_ids.add(article["id"])
        print(f"  Container created ({creation_id}), approval email sent.")

    state["drafted_article_ids"] = list(drafted_ids)
    save_state(state)


if __name__ == "__main__":
    main()
