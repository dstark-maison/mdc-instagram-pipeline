"""
Maison de Cocon — social draft stage (Instagram for now).

For each published blog article that has no ledger entry yet:
  1. generate the Instagram copy with Claude
  2. crop the featured image to 1:1 and push it (public raw URL, no hosting)
  3. save the draft to state/drafts/{article_id}/ig.json (numeric Shopify id)
  4. send ONE approval email (Approve / Reject)
  5. record status `draft` in state/ledger.json

Nothing is created on any platform at draft time.
"""

import os
import sys
from pathlib import Path

from core import approval, blog, config, copy, image, ledger
from channels import instagram

REQUIRED_ENV = [
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_BLOG_HANDLE", "ANTHROPIC_API_KEY",
    "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
    "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
]


def select_new_articles(articles, ledger_data, legacy_ids):
    return [
        a for a in articles
        if a["isPublished"]
        and ledger.numeric_id(a["id"]) not in legacy_ids
        and instagram.CODE not in ledger_data.get(ledger.numeric_id(a["id"]), {})
    ]


def draft_instagram(article, model):
    blog_handle = os.environ["SHOPIFY_BLOG_HANDLE"]
    handle = article["handle"]  # display + link only; the key is the numeric id
    article_id = ledger.numeric_id(article["id"])
    if not (article.get("image") or {}).get("url"):
        raise RuntimeError("Article has no featured image")

    article_url = blog.build_article_url(blog_handle, handle)
    content = copy.generate_instagram_caption(article, model)

    image_repo_path = f"generated/ig-{article_id}.jpg"
    image.crop_to_square(article["image"]["url"], Path(image_repo_path))
    # Pushed before the email so the preview URL is live.
    ledger.commit_and_push([image_repo_path], f"Add IG image for {article_id} ({handle}) [skip ci]")
    image_url = config.public_raw_url(image_repo_path)

    return instagram.build_draft(article, article_url, content, image_repo_path, image_url)


def main():
    config.require_env(REQUIRED_ENV)
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

    articles = blog.fetch_recent_articles(os.environ["SHOPIFY_BLOG_HANDLE"])
    new_articles = select_new_articles(articles, ledger.load(), ledger.legacy_drafted_ids())
    if not new_articles:
        print("No new articles to draft.")
        return

    failures = []
    for article in new_articles:
        print(f"Drafting for: {article['title']}")
        try:
            article_id = ledger.numeric_id(article["id"])
            draft = draft_instagram(article, model)
            approval.send_draft_email(
                {**article, "article_id": article_id},
                previews=[{
                    "label": "Instagram", "image_url": draft["image_url"],
                    "text": draft["full_caption"], "target": draft["target"],
                    "link": draft["article_url"],
                }],
                buttons=[("Approve", "approve", "ig"), ("Reject", "reject", "ig")],
            )

            def mutate(data, article=article, article_id=article_id):
                ledger.set_entry(
                    data, article_id, instagram.CODE,
                    status=ledger.DRAFT, article_id=article_id, handle=article["handle"],
                    post_id=None, url=None, timestamp=ledger.now(),
                )

            path = ledger.write_draft(article_id, instagram.CODE, draft)
            ledger.update(mutate, f"Draft {article_id} ({article['handle']}, ig) [skip ci]", [path])
            print("  Draft saved, approval email sent.")
        except Exception as exc:
            # One article must not block the rest. No ledger entry is written,
            # so it is retried next run.
            print(f"  FAILED to draft '{article['title']}': {exc}")
            failures.append((article["title"], exc))
            try:
                approval.send_failure_email(f"Draft failed (ig): {article['title']}", str(exc))
            except Exception as mail_exc:
                print(f"  (failure email also failed: {mail_exc})")

    if failures:
        print(f"\n{len(failures)} article(s) failed to draft")
        sys.exit(1)


if __name__ == "__main__":
    main()
