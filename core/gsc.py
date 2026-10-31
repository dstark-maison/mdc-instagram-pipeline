"""Search Console: a LIVE weekly pull of question-style queries (never stored in the repo).

Window = the same 7 days seo_report.py uses (end = today - 3, start = end - 6), property
sc-domain:maisondecocon.com, dimensions query + page, every row. Nothing here prints a query:
the repo is public, so logs show counts only.

Env: GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON or base64 of it, same format as the blog pipeline).
"""

import base64
import json
import os
from datetime import date, timedelta

import requests

from core import http, questions

SITE = "sc-domain:maisondecocon.com"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
ROW_LIMIT = 25000


class GscError(RuntimeError):
    pass


def credentials_info():
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        raise GscError("GOOGLE_SERVICE_ACCOUNT_JSON is not set (no Search Console access)")
    try:
        return json.loads(raw if raw.startswith("{") else base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise GscError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON/base64 JSON ({type(exc).__name__})") from None


def access_token():
    from google.oauth2 import service_account
    import google.auth.transport.requests
    info = credentials_info()
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    http.register_secret(creds.token)
    return creds.token


def window(today=None):
    today = today or date.today()
    end = today - timedelta(days=3)
    return end - timedelta(days=6), end


def fetch_rows(token, start, end, site=SITE):
    """All (query, page) rows for the window, paged by startRow."""
    rows, start_row = [], 0
    while True:
        resp = http.request(
            "POST",
            f"https://www.googleapis.com/webmasters/v3/sites/{requests.utils.quote(site, safe='')}/searchAnalytics/query",
            "gsc-query", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"startDate": str(start), "endDate": str(end), "dimensions": ["query", "page"],
                  "rowLimit": ROW_LIMIT, "startRow": start_row},
        )
        batch = resp.json().get("rows", [])
        rows += batch
        if len(batch) < ROW_LIMIT:
            return rows
        start_row += ROW_LIMIT


def aggregate(rows):
    """One record per query: impressions/clicks summed, impressions-weighted position, pages by impressions."""
    agg = {}
    for r in rows:
        query, page = r["keys"][0], r["keys"][1]
        a = agg.setdefault(query, {"query": query, "impressions": 0, "clicks": 0, "pos_weighted": 0.0, "pages": {}})
        imp = r.get("impressions", 0)
        a["impressions"] += imp
        a["clicks"] += r.get("clicks", 0)
        a["pos_weighted"] += r.get("position", 0) * imp
        a["pages"][page] = a["pages"].get(page, 0) + imp
    out = []
    for a in agg.values():
        imp = a["impressions"] or 1
        out.append({"query": a["query"], "impressions": a["impressions"], "clicks": a["clicks"],
                    "ctr": a["clicks"] / imp, "position": a["pos_weighted"] / imp,
                    "page": max(a["pages"], key=a["pages"].get)})
    return out


def question_queries(agg, min_impressions, max_ctr=0.02, pos_range=(5, 20)):
    """Question-style queries with impressions but low CTR, or ranking at position 5-20. Best first."""
    keep = [a for a in agg
            if a["impressions"] >= min_impressions and questions.is_question(a["query"])
            and (a["ctr"] < max_ctr or pos_range[0] <= a["position"] <= pos_range[1])]
    return sorted(keep, key=lambda a: (-a["impressions"], a["position"]))


def pull(today=None, min_impressions=3):
    """Live pull -> (all aggregated queries count, filtered question candidates, window)."""
    start, end = window(today)
    rows = fetch_rows(access_token(), start, end)
    agg = aggregate(rows)
    return {"rows": len(rows), "queries": len(agg), "candidates": question_queries(agg, min_impressions),
            "window": (start, end)}
