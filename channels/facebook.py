"""Facebook channel (Page photo post via the Graph API).

Same three steps as Instagram: the draft is saved at draft time, nothing is
created on the Page until publish time. The Page access token is fetched at
runtime from the system-user token (core.meta.page_credentials), cached for the
process only, redacted from every log/email, and never stored.
"""

import os
import time

from core import blog, copy, http, meta

CODE = "fb"
GRAPH_BASE = meta.GRAPH
UTM = "facebook"


def article_url_with_utm(blog_handle, handle):
    return blog.build_article_url(
        blog_handle, handle,
        utm={"utm_source": "facebook", "utm_medium": "social", "utm_campaign": handle},
    )


def build_draft(article, article_url, content, image_repo_path):
    """content = the IG content ({caption, hashtags}); article_url already carries the UTM."""
    return {
        "channel": CODE,
        "article_id": article["id"].rsplit("/", 1)[-1],
        "gid": article["id"],
        "handle": article["handle"],
        "title": article["title"],
        "published_at": article.get("publishedAt"),
        "article_url": article_url,
        "message": copy.facebook_message(content["caption"], content["hashtags"], article_url),
        "image_path": image_repo_path,  # image_url is built from the current repo name when needed
        "target": "Facebook Page (Maison de Cocon)",
    }


def _page_headers():
    _, _, token = meta.page_credentials()
    return {"Authorization": f"Bearer {token}"}


def verify(photo_id, draft, attempts=5, interval_s=3):
    """Read the photo post back by id. Its caption must match what we sent."""
    last = None
    for _ in range(attempts):
        try:
            info = http.request(
                "GET", f"{GRAPH_BASE}/{photo_id}", "fb-readback", headers=_page_headers(),
                params={"fields": "id,name,link,created_time"},
            ).json()
            if not info.get("link"):
                raise RuntimeError(f"Read-back has no link: {info}")
            sent, got = draft["message"].strip()[:80], (info.get("name") or "").strip()[:80]
            if sent != got:
                raise RuntimeError(f"Read-back message differs from draft: {got!r} != {sent!r}")
            return info
        except http.ApiError as exc:
            last = exc
            time.sleep(interval_s)
    raise last


def publish(draft, mark_publishing, revert_publishing):
    """Returns {post_id, url, timestamp}. Raises on any failure.

    mark_publishing() persists `publishing` BEFORE the photo is posted, so a crash
    afterwards cannot lead to an automatic second post. If Facebook answers the
    post with an HTTP error nothing was posted, so revert_publishing() puts the
    ledger back to `draft` and only this channel can be retried.
    """
    page_id, _, _ = meta.page_credentials()
    mark_publishing(None)
    try:
        resp = http.request(
            "POST", f"{GRAPH_BASE}/{page_id}/photos", "fb-photo-publish", headers=_page_headers(),
            data={"url": draft["image_url"], "message": draft["message"]},
        )
    except http.ApiError:
        revert_publishing()
        raise
    photo_id = resp.json()["id"]
    info = verify(photo_id, draft)
    return {"post_id": photo_id, "url": info["link"], "timestamp": info.get("created_time")}


def after_publish(draft, result):
    return None  # nothing follows a Facebook post (the bio link is an Instagram concern)
