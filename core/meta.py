"""Meta token preflight (debug_token).

Run at the start of every draft and publish run. If the token is invalid, expired
or missing a required scope, the run stops and a failure email names the problem.

The system-user token is sent as a bearer header for Instagram calls. debug_token
takes the inspected token as a query parameter, so every error text goes through
http.redact() and request failures never echo the URL (see http.request).
"""

import os

from core import http

GRAPH = "https://graph.facebook.com/v25.0"

# Phase 1. pages_manage_posts is added in Phase 2, not before.
REQUIRED_SCOPES = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"]


class TokenError(RuntimeError):
    pass


def preflight(required=None):
    required = required or REQUIRED_SCOPES
    token = os.environ.get("ACCESS_TOKEN")
    if not token:
        raise TokenError("Meta token is missing (ACCESS_TOKEN is empty; secret META_SYSTEM_USER_TOKEN).")
    try:
        resp = http.request(
            "GET", f"{GRAPH}/debug_token", "meta-debug-token",
            headers={"Authorization": f"Bearer {token}"}, params={"input_token": token},
        )
    except http.ApiError as exc:
        raise TokenError(f"Meta token is invalid or cannot be inspected: {exc}") from None
    data = resp.json().get("data", {})
    if not data.get("is_valid"):
        raise TokenError(f"Meta token is not valid: {http.redact(data.get('error') or data)}")
    expires = data.get("expires_at") or data.get("data_access_expires_at")
    scopes = set(data.get("scopes") or [])
    missing = [s for s in required if s not in scopes]
    if missing:
        raise TokenError("Meta token is missing required scope(s): " + ", ".join(missing)
                         + f". Token type {data.get('type')}; scopes present: {', '.join(sorted(scopes))}.")
    print(f"  Meta token OK: type={data.get('type')}, expires_at={data.get('expires_at')}, "
          f"scopes ok ({', '.join(required)})")
    return data


if __name__ == "__main__":
    # Manual check: Actions -> Social Token Check. Failure sends the failure email.
    from core import approval, config
    config.require_env(["ACCESS_TOKEN", "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO", "GITHUB_REPOSITORY"])
    try:
        preflight()
    except TokenError as exc:
        print(http.redact(exc))
        approval.send_failure_email("Meta token check failed", str(exc))
        raise SystemExit(1)
