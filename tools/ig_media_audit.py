"""
READ-ONLY audit: which legacy drafts are actually live on Instagram?

Posts nothing, writes nothing. It
  1. lists the IG account's published media (timestamp, first caption line),
  2. perceptual-hashes each media image and every generated/ig-*.jpg we ever
     posted (the exact file used as the container image),
  3. validates the method on the posts whose permalinks are in the old publish
     logs (tools/legacy_map.json -> known_permalinks),
  4. reports matches for the backlog articles and scans their bodies for
     down-product claims,
  5. prints a machine-readable block between ===JSON=== markers.

Env: ACCESS_TOKEN, IG_USER_ID, SHOPIFY_* (read_content).
"""

import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from core import http, shopify_client

GRAPH = "https://graph.facebook.com/v25.0"
MAP = json.loads(Path("tools/legacy_map.json").read_text())
BACKLOG = json.loads(Path("tools/backlog26_ids.json").read_text())
MAX_DIST, MIN_MARGIN = 10, 6


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return bits


def dist(a, b):
    return bin(a ^ b).count("1")


def fetch_image(url):
    return Image.open(io.BytesIO(http.request("GET", url, "image").content)).convert("RGB")


def list_media():
    import os
    headers = {"Authorization": f"Bearer {os.environ['ACCESS_TOKEN']}"}
    params = {"fields": "id,caption,permalink,timestamp,media_type,media_url,thumbnail_url", "limit": 50}
    media, after = [], None
    while True:
        p = dict(params, **({"after": after} if after else {}))
        data = http.request("GET", f"{GRAPH}/{os.environ['IG_USER_ID']}/media", "ig-media-list",
                            headers=headers, params=p).json()
        media += data.get("data", [])
        after = data.get("paging", {}).get("cursors", {}).get("after")
        if not data.get("paging", {}).get("next") or not after:
            return media


def article_bodies(ids):
    out = {}
    for i in range(0, len(ids), 25):
        chunk = [f"gid://shopify/Article/{x}" for x in ids[i:i + 25]]
        data = shopify_client.graphql(
            "query($ids:[ID!]!){nodes(ids:$ids){... on Article{id handle title publishedAt body}}}",
            {"ids": chunk})
        for n in data["nodes"]:
            if n:
                out[n["id"].rsplit("/", 1)[-1]] = n
    return out


DOWN_RE = re.compile(r"\b(down|goose|duck|feather|daunen|federn|gänse|enten)\w*", re.I)


def down_hits(body):
    text = re.sub(r"<[^>]+>", " ", body or "")
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if DOWN_RE.search(s)]
    return len(sents), [s[:160] for s in sents[:3]]


def main():
    h2id = MAP["handle_to_id"]
    local = {}
    for p in sorted(Path("generated").glob("ig-*.jpg")):
        handle = p.stem[3:]
        local[handle] = dhash(Image.open(p))
    print(f"{len(local)} local images hashed")

    media = list_media()
    print(f"{len(media)} IG media found\n")
    rows = []
    for m in media:
        url = m.get("media_url") if m.get("media_type") == "IMAGE" else (m.get("thumbnail_url") or m.get("media_url"))
        first = (m.get("caption") or "").strip().split("\n")[0][:90]
        row = {"id": m["id"], "timestamp": m["timestamp"], "permalink": m["permalink"],
               "type": m.get("media_type"), "first_line": first, "match": None}
        try:
            h = dhash(fetch_image(url))
            ranked = sorted((dist(h, v), k) for k, v in local.items())
            (d1, k1), (d2, _) = ranked[0], ranked[1]
            row.update(best=k1, dist=d1, margin=d2 - d1)
            if d1 <= MAX_DIST and (d2 - d1) >= MIN_MARGIN:
                row["match"] = k1
        except Exception as exc:  # report, never guess
            row["error"] = str(exc)[:120]
        rows.append(row)

    # --- validation on posts with a known publish log
    known = MAP["known_permalinks"]
    by_perma = {r["permalink"]: r for r in rows}
    ok = bad = missing = 0
    for perma, handle in known.items():
        r = by_perma.get(perma)
        if not r:
            missing += 1
        elif r["match"] == handle:
            ok += 1
        else:
            bad += 1
            print(f"  VALIDATION MISMATCH {perma}: expected {handle}, got {r.get('best')} d={r.get('dist')} margin={r.get('margin')}")
    print(f"VALIDATION: {ok} of {len(known)} known posts matched their image, {bad} wrong, {missing} not in media list\n")

    # --- every media row
    print("timestamp            | match (dist/margin)                         | first caption line")
    for r in sorted(rows, key=lambda r: r["timestamp"]):
        tag = f"{r['match']} ({r.get('dist')}/{r.get('margin')})" if r["match"] else f"-- best {r.get('best')} ({r.get('dist')}/{r.get('margin')})"
        print(f"{r['timestamp'][:19]} | {tag[:44]:44} | {r['first_line']}")

    # --- backlog
    id2handle = {v: k for k, v in h2id.items()}
    live = {}
    for r in rows:
        if r["match"] and h2id.get(r["match"]) in BACKLOG:
            live.setdefault(h2id[r["match"]], []).append(r)
    bodies = article_bodies(BACKLOG)
    print(f"\nBACKLOG ({len(BACKLOG)} articles): {len(live)} live on Instagram")
    result = []
    for aid in BACKLOG:
        a = bodies.get(aid, {})
        n, sents = down_hits(a.get("body"))
        hit = live.get(aid)
        print(f"- {aid} {id2handle.get(aid)} | LIVE: {hit[0]['permalink'] + ' ' + hit[0]['timestamp'][:10] if hit else 'no'} | down-sentences: {n}")
        for s in sents:
            print(f"      > {s}")
        result.append({"article_id": aid, "handle": id2handle.get(aid), "current_handle": a.get("handle"),
                       "title": a.get("title"), "live": [{"post_id": r["id"], "url": r["permalink"], "timestamp": r["timestamp"]} for r in hit or []],
                       "down_sentences": n})
    unmatched = [r for r in rows if not r["match"]]
    print(f"\n{len(unmatched)} IG media with no confident image match (not necessarily legacy posts)")
    print("===JSON===")
    print(json.dumps({"backlog": result, "unmatched": [{k: r.get(k) for k in ("id", "timestamp", "permalink", "first_line", "best", "dist", "margin")} for r in unmatched]}))
    print("===END===")


if __name__ == "__main__":
    sys.exit(main())
