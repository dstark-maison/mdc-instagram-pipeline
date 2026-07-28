"""
Shared Shopify Admin API access for the IG pipeline.

client_credentials grant only — this store's app is a Dev Dashboard app,
which does not expose a static Admin API token (mirrors the same pattern
used in mdc-blog-pipeline/src/lib/shopify.js). The token is fetched fresh
at the start of each process run and cached only for that run's lifetime.

Required environment variables:
  SHOPIFY_STORE_DOMAIN     e.g. "zpjzx0-gy.myshopify.com"
  SHOPIFY_CLIENT_ID
  SHOPIFY_CLIENT_SECRET
"""

import os

import requests

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
    resp = requests.post(
        f"https://{domain}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": _env("SHOPIFY_CLIENT_ID"),
            "client_secret": _env("SHOPIFY_CLIENT_SECRET"),
        },
    )
    if not resp.ok:
        raise RuntimeError(
            f"Shopify token request failed: {resp.status_code} {resp.text}\n"
            "If this says 'shop_not_permitted', the app and store aren't in "
            "the same Shopify organization."
        )
    _cached_token = resp.json()["access_token"]
    return _cached_token


def graphql(query, variables=None):
    domain = _env("SHOPIFY_STORE_DOMAIN")
    token = get_access_token()

    resp = requests.post(
        f"https://{domain}/admin/api/{API_VERSION}/graphql.json",
        json={"query": query, "variables": variables or {}},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]
