"""Blog source: Shopify articles."""

import os

from core import shopify_client


def fetch_recent_articles(blog_handle, limit=10):
    """Most recent blog articles via the Admin GraphQL API.

    QueryRoot has no `blogByHandle` (confirmed against the live schema), so
    the blog is looked up via `blogs(query: "handle:...")`. Blog.articles has
    no sortKey either — reverse:true on the default (ID) order matches
    publish order for a blog that only publishes forward in time.
    """
    query = """
    query RecentArticles($blogHandleQuery: String!, $first: Int!) {
      blogs(first: 1, query: $blogHandleQuery) {
        nodes {
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
    }
    """
    data = shopify_client.graphql(
        query, {"blogHandleQuery": f"handle:{blog_handle}", "first": limit}
    )
    blogs = data["blogs"]["nodes"]
    if not blogs:
        raise RuntimeError(f"Blog handle '{blog_handle}' not found or no access")
    return blogs[0]["articles"]["nodes"]


def build_article_url(blog_handle, article_handle, utm=None):
    storefront = os.environ.get("STOREFRONT_DOMAIN", "www.maisondecocon.com")
    url = f"https://{storefront}/blogs/{blog_handle}/{article_handle}"
    if utm:
        url += "?" + "&".join(f"{k}={v}" for k, v in utm.items())
    return url


def fetch_article(article_id):
    """One article by NUMERIC id (used for explicit, on-request drafts)."""
    data = shopify_client.graphql(
        """query($id: ID!) { article(id: $id) {
             id title handle publishedAt isPublished summary body image { url } } }""",
        {"id": f"gid://shopify/Article/{article_id}"},
    )
    if not data["article"]:
        raise RuntimeError(f"No article with id {article_id}")
    return data["article"]
