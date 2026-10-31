"""HTTP helpers shared by every channel.

check() prints the response body BEFORE raising, so the platform's own error
text always reaches the Actions log (raise_for_status() alone hides it).
"""

import os
import re

import requests

# Any env var whose NAME looks like a credential is redacted from logs, emails
# and issue comments (covers ACCESS_TOKEN, RESEND_API_KEY, SHOPIFY_CLIENT_SECRET,
# GITHUB_TOKEN, ...). Runtime-fetched tokens must call register_secret().
SECRET_NAME_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD", re.I)
_runtime_secrets = set()


def register_secret(value):
    if value:
        _runtime_secrets.add(value)


class ApiError(RuntimeError):
    """A non-2xx platform response. str(exc) carries the raw body."""


def redact(text):
    text = str(text)
    values = {v for n, v in os.environ.items() if SECRET_NAME_RE.search(n)} | _runtime_secrets
    for value in sorted(values, key=len, reverse=True):
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
    try:
        resp = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        # str(exc) can contain the full URL (query strings with tokens).
        raise ApiError(f"{label} failed: {type(exc).__name__}: {redact(exc)}") from None
    return check(resp, label)
