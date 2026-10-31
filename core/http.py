"""HTTP helpers shared by every channel.

check() prints the response body BEFORE raising, so the platform's own error
text always reaches the Actions log (raise_for_status() alone hides it).
"""

import os

import requests

# Env vars whose values must never appear in logs, emails or issue comments.
SECRET_ENV_NAMES = [
    "ACCESS_TOKEN", "RESEND_API_KEY", "ANTHROPIC_API_KEY",
    "SHOPIFY_CLIENT_SECRET", "GITHUB_TOKEN", "GH_TOKEN",
]


class ApiError(RuntimeError):
    """A non-2xx platform response. str(exc) carries the raw body."""


def redact(text):
    text = str(text)
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return text


def check(resp, label):
    if resp.ok:
        print(f"  [{label}] {resp.status_code}")
        return resp
    print(f"  [{label}] {resp.status_code}: {redact(resp.text)[:4000]}")
    raise ApiError(f"{label} failed: HTTP {resp.status_code}: {redact(resp.text)}")


def request(method, url, label, **kwargs):
    kwargs.setdefault("timeout", 60)
    return check(requests.request(method, url, **kwargs), label)
