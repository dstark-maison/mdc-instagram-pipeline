"""Our blog articles as the ONLY fact source for Reddit answers: index, FAQ blocks, matching.

* fetch_index(): every article with body + German handle/title (Shopify translations).
  Translations need the read_translations scope; without it the index still works, German
  Search Console pages are then matched by title overlap instead of by handle.
* faq_pairs(): the FAQPage JSON-LD in the article body (fallback: the accordion markup).
* match_page() / match_text(): which article a Search Console page or a free question belongs to.
* held articles (config.HOLD_ARTICLE_IDS, the down-claim ones) are never a link target or fact source.
"""

import json
import os
import re
from urllib.parse import quote, urlparse

from core import config, shopify_client

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "is", "are", "what", "which", "how", "why", "when", "do",
    "does", "can", "should", "my", "your", "you", "i", "it", "with", "at", "best", "vs", "versus", "between", "difference",
    "der", "die", "das", "den", "dem", "ein", "eine", "und", "oder", "ist", "sind", "was", "wie", "welche", "welcher",
    "welches", "warum", "wieso", "für", "mit", "bei", "von", "zu", "im", "auf", "man", "mein", "meine", "unterschied",
    "zwischen", "gleiche", "besser", "wann", "wo", "wer", "kann", "soll", "ich", "nicht", "bettwäsche", "bett", "bedding",
}
MIN_SHARED_TOKENS = 2

ARTICLES_QUERY = """
query($q: String!, $after: String) {
  blogs(first: 1, query: $q) { nodes { articles(first: 50, after: $after) {
    nodes { id handle title publishedAt isPublished body }
    pageInfo { hasNextPage endCursor } } } }
}"""
TRANSLATIONS_QUERY = """
query($q: String!, $after: String) {
  blogs(first: 1, query: $q) { nodes { articles(first: 50, after: $after) {
    nodes { id translations(locale: "de") { key value } }
    pageInfo { hasNextPage endCursor } } } }
}"""


def _pages(query, blog_handle):
    after = None
    while True:
        data = shopify_client.graphql(query, {"q": f"handle:{blog_handle}", "after": after})
        blogs = data["blogs"]["nodes"]
        if not blogs:
            raise RuntimeError(f"Blog handle '{blog_handle}' not found or no access")
        page = blogs[0]["articles"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


def fetch_index(blog_handle):
    arts = {}
    for a in _pages(ARTICLES_QUERY, blog_handle):
        arts[a["id"].rsplit("/", 1)[-1]] = {
            "id": a["id"].rsplit("/", 1)[-1], "handle": a["handle"], "title": a["title"], "body": a.get("body") or "",
            "published": bool(a["isPublished"]), "published_at": a.get("publishedAt"), "de_handle": None, "de_title": None}
    try:
        for node in _pages(TRANSLATIONS_QUERY, blog_handle):
            tr = {t["key"]: t["value"] for t in node.get("translations", [])}
            art = arts.get(node["id"].rsplit("/", 1)[-1])
            if art:
                art["de_handle"], art["de_title"] = tr.get("handle"), tr.get("title")
    except Exception as exc:  # most likely a missing read_translations scope
        print(f"  (German handles unavailable, matching by title overlap: {str(exc)[:100]})")
    return list(arts.values())


# --- FAQ ----------------------------------------------------------------------

_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_ACCORDION_RE = re.compile(r"<summary>.*?<span>(.*?)</span>.*?</summary>.*?<div class=\"prose\">(.*?)</div>", re.S)


def strip_html(html):
    html = re.sub(r"<!--.*?-->|<script.*?</script>|<style.*?</style>", " ", html or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def faq_pairs(body):
    """[(question, answer)] from the article's FAQ block."""
    pairs = []
    for m in _LD_RE.finditer(body or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("@type") == "FAQPage":
                for q in node.get("mainEntity", []):
                    name, ans = q.get("name"), (q.get("acceptedAnswer") or {}).get("text")
                    if name and ans:
                        pairs.append((strip_html(name), strip_html(ans)))
    if not pairs:
        pairs = [(strip_html(q), strip_html(a)) for q, a in _ACCORDION_RE.findall(body or "")]
    return pairs


def source_text(article, limit=7000):
    """The facts a draft may use: the article text without markup/placeholders, FAQ included once."""
    body = re.sub(r"<script type=\"application/ld\+json\">.*?</script>", " ", article["body"], flags=re.S)
    text = strip_html(body).replace("[[mdc-products]]", "")
    faq = " ".join(f"Q: {q} A: {a}" for q, a in faq_pairs(article["body"]))
    return (text + (" FAQ: " + faq if faq and faq[:80] not in text else ""))[:limit]


# --- matching -----------------------------------------------------------------

def tokens(text):
    return {t for t in re.findall(r"[\wäöüß]+", (text or "").lower()) if len(t) > 2 and t not in STOPWORDS and not t.isdigit()}


def eligible(index, hold=None):
    hold = config.HOLD_ARTICLE_IDS if hold is None else hold
    return [a for a in index if a["published"] and a["id"] not in hold]


def handle_of_url(url):
    m = re.search(r"/blogs/[^/]+/([^/?#]+)", urlparse(url or "").path)
    return m.group(1).lower() if m else None


def match_page(url, index):
    """Article for a Search Console page URL (English or German handle). Held articles ARE returned
    here so the caller can see and skip them."""
    handle = handle_of_url(url)
    if not handle:
        return None
    return next((a for a in index if handle in (a["handle"].lower(), (a["de_handle"] or "").lower())), None)


def match_text(question, index, min_shared=MIN_SHARED_TOKENS):
    """Best article for a free question by distinctive-token overlap with handle + titles; None if weak."""
    q = tokens(question)
    best, best_score = None, 0
    for a in index:
        bag = tokens(a["handle"].replace("-", " ")) | tokens(a["title"]) | tokens(a["de_title"]) | tokens((a["de_handle"] or "").replace("-", " "))
        score = len(q & bag)
        if score > best_score:
            best, best_score = a, score
    return best if best_score >= min_shared else None


def article_url(article, lang, utm=None):
    """Link in the question's language: German pages live at /blogs/blog/<de-handle>, English at
    /en/blogs/blog/<handle>. utm_campaign is always the article's primary handle."""
    storefront = os.environ.get("STOREFRONT_DOMAIN", "www.maisondecocon.com")
    blog = os.environ.get("SHOPIFY_BLOG_HANDLE", "blog")
    if lang == "de" and article.get("de_handle"):
        url = f"https://{storefront}/blogs/{blog}/{article['de_handle']}"
    else:
        url = f"https://{storefront}/en/blogs/{blog}/{article['handle']}"
    if utm:
        url += "?" + "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in utm.items())
    return url


def reddit_utm(article):
    return {"utm_source": "reddit", "utm_medium": "social", "utm_campaign": article["handle"]}
