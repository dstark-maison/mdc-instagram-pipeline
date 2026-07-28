"""
Maison de Cocon — Instagram publish stage.

Triggered ONLY by manually running the ig-publish.yml GitHub Actions
workflow, after reviewing the approval email from ig_draft.py. Takes the
creation_id and blog_post_url as workflow_dispatch inputs.

Two things happen here, in order:
  1. The held media container is published to Instagram — this is the
     one irreversible, "goes live" step in the whole pipeline.
  2. The Shopify redirect that the Instagram bio link points to is
     updated to forward to this specific blog post.

Why a redirect and not editing the bio directly: the Instagram Graph API
has no endpoint to write the bio/website field — it's editable only by hand,
inside the Instagram app. So the bio link is set ONCE, manually, to a fixed
address (e.g. https://www.maisondecocon.com/pages/latest-post), and this
script keeps updating a Shopify redirect behind that fixed address instead.

Required environment variables:
  ACCESS_TOKEN             Meta System User / Page-scoped access token
  IG_USER_ID               Instagram Business Account ID
  SHOPIFY_STORE_DOMAIN
  SHOPIFY_CLIENT_ID        Shopify custom app credentials (client_credentials
  SHOPIFY_CLIENT_SECRET    grant — see shopify_client.py)
  BIO_LINK_PATH            the fixed path the redirect lives at, e.g.
                           "/pages/latest-post" (must match what's actually
                           set as the Instagram bio link)

Inputs (passed as CLI args by the workflow):
  creation_id
  blog_post_url
"""

import os
import sys
import time

import requests

import shopify_client

GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

REQUIRED_ENV = [
    "ACCESS_TOKEN", "IG_USER_ID",
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "BIO_LINK_PATH",
]


def require_env():
    missing = [n for n in REQUIRED_ENV if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def wait_until_ready(access_token, creation_id, timeout_s=60, interval_s=3):
    """Poll the container's status before publishing — mirrors the tested,
    working flow in ig_publish_test.py. Usually a no-op by the time a human
    has reviewed the approval email, but cheap insurance if approval happens
    within seconds of the container being created."""
    waited = 0
    while waited < timeout_s:
        resp = requests.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Container processing failed (status_code=ERROR)")
        time.sleep(interval_s)
        waited += interval_s
    raise TimeoutError("Container did not finish processing in time")


def publish_container(ig_user_id, access_token, creation_id):
    resp = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def confirm_published(access_token, post_id):
    resp = requests.get(
        f"{GRAPH_BASE}/{post_id}",
        params={"fields": "permalink,timestamp", "access_token": access_token},
    )
    resp.raise_for_status()
    return resp.json()


def find_existing_redirect(path):
    """Look up whether a Shopify redirect already exists for this path.

    The query filter needs an explicit `path:` field prefix (confirmed
    against Shopify's search-syntax docs) — a bare path string falls back to
    an unreliable full-text search across multiple fields instead of
    matching the path field specifically.
    """
    query = """
    query FindRedirect($query: String!) {
      urlRedirects(first: 1, query: $query) {
        nodes { id path target }
      }
    }
    """
    data = shopify_client.graphql(query, {"query": f"path:{path}"})
    nodes = data["urlRedirects"]["nodes"]
    return nodes[0] if nodes else None


def upsert_redirect(path, target_url):
    """Requires the write_online_store_navigation scope on the Shopify app
    (not write_content — confirmed against Shopify's mutation docs)."""
    existing = find_existing_redirect(path)

    if existing:
        mutation = """
        mutation UpdateRedirect($id: ID!, $target: String!) {
          urlRedirectUpdate(id: $id, urlRedirect: {target: $target}) {
            urlRedirect { id path target }
            userErrors { field message }
          }
        }
        """
        variables = {"id": existing["id"], "target": target_url}
        result_key = "urlRedirectUpdate"
    else:
        mutation = """
        mutation CreateRedirect($path: String!, $target: String!) {
          urlRedirectCreate(urlRedirect: {path: $path, target: $target}) {
            urlRedirect { id path target }
            userErrors { field message }
          }
        }
        """
        variables = {"path": path, "target": target_url}
        result_key = "urlRedirectCreate"

    data = shopify_client.graphql(mutation, variables)[result_key]
    if data["userErrors"]:
        raise RuntimeError(f"Shopify redirect error: {data['userErrors']}")
    return data["urlRedirect"]


def main():
    require_env()

    if len(sys.argv) != 3:
        print("Usage: python ig_publish.py <creation_id> <blog_post_url>")
        sys.exit(1)

    creation_id, blog_post_url = sys.argv[1], sys.argv[2]

    print(f"Checking container {creation_id} is ready...")
    wait_until_ready(os.environ["ACCESS_TOKEN"], creation_id)

    print(f"Publishing container {creation_id}...")
    post_id = publish_container(os.environ["IG_USER_ID"], os.environ["ACCESS_TOKEN"], creation_id)
    info = confirm_published(os.environ["ACCESS_TOKEN"], post_id)
    print(f"  Published. Permalink: {info.get('permalink')}")

    print(f"Updating bio-link redirect to point to {blog_post_url}...")
    redirect = upsert_redirect(os.environ["BIO_LINK_PATH"], blog_post_url)
    print(f"  Redirect '{redirect['path']}' now points to '{redirect['target']}'.")

    print("\nDone. Instagram post is live and the bio link now points to this post.")


if __name__ == "__main__":
    main()
