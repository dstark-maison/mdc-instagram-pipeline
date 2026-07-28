"""
Maison de Cocon — Instagram Graph API publish test script.

Purpose: a minimal, real (not mocked) end-to-end test of the publish flow,
used both to validate the integration and as the working flow to screen-record
for Meta App Review's required screencast.

Flow (each step annotated with the permission it exercises — say these out
loud when recording, per Meta's App Review guidance):

  1. instagram_basic          -> confirm we can read the connected IG account
  2. instagram_content_publish -> create a media container (not yet public)
  3. instagram_content_publish -> poll the container until ready
  4. instagram_content_publish -> publish the container (this goes live)
  5. instagram_basic          -> read back the published post to confirm

Usage:
  pip install requests --break-system-packages
  export IG_USER_ID=17841473943836366
  export ACCESS_TOKEN=<your token — see note below>
  python ig_publish_test.py

Note on ACCESS_TOKEN: for this test, the short-lived User Token generated in
Graph API Explorer (with instagram_basic, instagram_content_publish,
business_management, pages_show_list, pages_read_engagement) is sufficient.
Production pipeline should use a long-lived Page access token instead —
that's a separate, not-yet-resolved open item in the spec doc.
"""

import os
import sys
import time
import requests

GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

IG_USER_ID = os.environ.get("IG_USER_ID")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

# A publicly reachable test image. Swap for a real Nano Banana-generated
# image or a blog post's featured image URL once wired into the real pipeline.
TEST_IMAGE_URL = os.environ.get(
    "TEST_IMAGE_URL", "https://picsum.photos/1080/1080"
)
TEST_CAPTION = os.environ.get(
    "TEST_CAPTION",
    "Test post — Maison de Cocon Instagram publishing pipeline. "
    "(Posted via Graph API integration test, safe to delete.)",
)


def require_env():
    missing = [n for n in ("IG_USER_ID", "ACCESS_TOKEN") if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def step_1_confirm_account():
    """instagram_basic — confirm we can read the connected account."""
    print("\n[1] instagram_basic — reading connected account info...")
    resp = requests.get(
        f"{GRAPH_BASE}/{IG_USER_ID}",
        params={"fields": "username,id", "access_token": ACCESS_TOKEN},
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"    Connected account: @{data.get('username')} (id: {data.get('id')})")
    return data


def step_2_create_container():
    """instagram_content_publish — create a media container (not yet public)."""
    print("\n[2] instagram_content_publish — creating media container...")
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={
            "image_url": TEST_IMAGE_URL,
            "caption": TEST_CAPTION,
            "access_token": ACCESS_TOKEN,
        },
    )
    resp.raise_for_status()
    creation_id = resp.json()["id"]
    print(f"    Container created: {creation_id} (not published yet)")
    return creation_id


def step_3_wait_until_ready(creation_id, timeout_s=60, interval_s=3):
    """instagram_content_publish — poll container status before publishing."""
    print("\n[3] instagram_content_publish — polling container status...")
    waited = 0
    while waited < timeout_s:
        resp = requests.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": ACCESS_TOKEN},
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        print(f"    status_code: {status}")
        if status == "FINISHED":
            return True
        if status == "ERROR":
            raise RuntimeError("Container processing failed (status_code=ERROR)")
        time.sleep(interval_s)
        waited += interval_s
    raise TimeoutError("Container did not finish processing in time")


def step_4_publish(creation_id):
    """instagram_content_publish — publish the container. This goes live."""
    print("\n[4] instagram_content_publish — publishing (this goes live)...")
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": ACCESS_TOKEN},
    )
    resp.raise_for_status()
    post_id = resp.json()["id"]
    print(f"    Published. Post id: {post_id}")
    return post_id


def step_5_confirm_published(post_id):
    """instagram_basic — read back the published post to confirm."""
    print("\n[5] instagram_basic — reading back the published post...")
    resp = requests.get(
        f"{GRAPH_BASE}/{post_id}",
        params={
            "fields": "id,caption,media_url,permalink,timestamp",
            "access_token": ACCESS_TOKEN,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"    Permalink: {data.get('permalink')}")
    print(f"    Timestamp: {data.get('timestamp')}")
    return data


def main():
    require_env()
    step_1_confirm_account()
    creation_id = step_2_create_container()
    step_3_wait_until_ready(creation_id)
    post_id = step_4_publish(creation_id)
    result = step_5_confirm_published(post_id)
    print("\nDone. Full end-to-end publish flow confirmed working.")
    print(f"Permalink to review / delete afterward: {result.get('permalink')}")


if __name__ == "__main__":
    main()
