"""
Shared Shopify Admin API access for the social pipeline.

client_credentials grant only — this store's app is a Dev Dashboard app,
which does not expose a static Admin API token. The token is fetched fresh
at the start of each process run and cached only for that run's lifetime.

Required environment variables:
  SHOPIFY_STORE_DOMAIN     e.g. "your-store.myshopify.com"
  SHOPIFY_CLIENT_ID
  SHOPIFY_CLIENT_SECRET
"""

import os

from core import http

API_VERSION = "2025-01"

_cached_token = None


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def get_access_token():
    global _cached_token
    if _cached_token:
        return _cached_token

    domain = _env("SHOPIFY_STORE_DOMAIN")
    resp = http.request(
        "POST", f"https://{domain}/admin/oauth/access_token", "shopify-token",
        data={
            "grant_type": "client_credentials",
            "client_id": _env("SHOPIFY_CLIENT_ID"),
            "client_secret": _env("SHOPIFY_CLIENT_SECRET"),
        },
    )
    _cached_token = resp.json()["access_token"]
    return _cached_token


def graphql(query, variables=None):
    domain = _env("SHOPIFY_STORE_DOMAIN")
    token = get_access_token()

    resp = http.request(
        "POST", f"https://{domain}/admin/api/{API_VERSION}/graphql.json", "shopify-graphql",
        json={"query": query, "variables": variables or {}},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
    )
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]
