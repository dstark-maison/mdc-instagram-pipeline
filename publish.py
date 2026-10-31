"""
Maison de Cocon — social publish / reject stage.

Input: env TITLE, shaped exactly "<approve|reject>:<handle>:<channels>"
(validated with a strict regex; anything else fails). It comes from an
approval issue title or from workflow_dispatch inputs — never from a shell
command line.

approve  → publish each channel, read it back by id, write the ledger.
           Already `published` → no-op.
reject   → mark the draft `rejected` in the ledger. No publish call.

The outcome is written to RESULT_FILE (markdown) for the workflow to post on
the approval issue. Any failure also sends a failure email with the raw error.
"""

import os
import sys
from pathlib import Path

from core import approval, config, http, ledger
from core.approval import ACTION_RE
from channels import instagram

CHANNELS = {instagram.CODE: instagram}

REQUIRED_ENV = ["GITHUB_REPOSITORY", "GITHUB_REF_NAME", "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO"]
IG_ENV = ["ACCESS_TOKEN", "IG_USER_ID", "SHOPIFY_STORE_DOMAIN",
          "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "BIO_LINK_PATH"]


def parse_title(title):
    if not ACTION_RE.fullmatch(title or ""):
        raise ValueError(f"Rejected input: {title!r} does not match the approval format.")
    action, handle, channels = title.split(":")
    return action, handle, channels.split("+")


def do_reject(handle, channel):
    data = ledger.load()
    entry = ledger.get(data, handle, channel)
    if not entry:
        return f"- `{channel}`: no draft found, nothing to reject."
    if entry["status"] != ledger.DRAFT:
        return f"- `{channel}`: status is `{entry['status']}`, left unchanged."

    def mutate(d):
        ledger.set_entry(d, handle, channel, status=ledger.REJECTED, timestamp=ledger.now())

    ledger.update(mutate, f"Reject {handle} ({channel}) [skip ci]")
    return f"- `{channel}`: draft rejected."


def do_approve(handle, channel):
    """Returns (markdown line, ok)."""
    module = CHANNELS.get(channel)
    if not module:
        return f"- `{channel}`: not implemented in this phase.", False

    data = ledger.load()
    entry = ledger.get(data, handle, channel)
    if not entry:
        return f"- `{channel}`: no draft in the ledger for `{handle}`.", False
    if entry["status"] == ledger.PUBLISHED:
        return f"- `{channel}`: already published, nothing done → {entry['url']}", True
    if entry["status"] == ledger.PUBLISHING:
        return (f"- `{channel}`: a previous run reached the publish call but was never "
                f"verified (container `{entry.get('container_id')}`). Not retrying "
                f"automatically — check the platform, then fix the ledger by hand."), False
    if entry["status"] == ledger.REJECTED:
        return f"- `{channel}`: this draft was rejected; it will not be published.", False

    draft = ledger.read_draft(handle, channel)

    def mark_publishing(container_id):
        def mutate(d):
            ledger.set_entry(d, handle, channel, status=ledger.PUBLISHING,
                             container_id=container_id, timestamp=ledger.now())
        ledger.update(mutate, f"Publishing {handle} ({channel}) [skip ci]")

    def revert_publishing():
        def mutate(d):
            ledger.set_entry(d, handle, channel, status=ledger.DRAFT, container_id=None,
                             timestamp=ledger.now())
        ledger.update(mutate, f"Publish refused for {handle} ({channel}), back to draft [skip ci]")

    result = module.publish(draft, mark_publishing, revert_publishing)

    def mutate(d):
        ledger.set_entry(d, handle, channel, status=ledger.PUBLISHED, post_id=result["post_id"],
                         url=result["url"], timestamp=result["timestamp"] or ledger.now())

    ledger.update(mutate, f"Published {handle} ({channel}) [skip ci]")
    line = f"- `{channel}`: published and verified → {result['url']}"

    ok = True
    try:
        extra = module.after_publish(draft, result)
        if extra:
            line += f"\n  - {extra}"
    except Exception as exc:
        ok = False
        line += f"\n  - **post-publish step failed** (the post itself is live): {http.redact(exc)}"
        approval.send_failure_email(f"Post live, follow-up step failed ({channel}): {handle}", str(exc))
    return line, ok


def write_result(text):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = os.environ.get("RESULT_FILE")
    if path:
        Path(path).write_text(text, encoding="utf-8")
    print(text)


def main():
    config.require_env(REQUIRED_ENV)
    title = os.environ.get("TITLE", "")
    try:
        action, handle, channels = parse_title(title)
    except ValueError as exc:
        write_result(f"❌ {exc}")
        sys.exit(1)

    lines, failed = [], False
    if action == "reject":
        for ch in channels:
            lines.append(do_reject(handle, ch))
        write_result(f"**Reject `{handle}`**\n\n" + "\n".join(lines))
        return

    if any(ch in CHANNELS for ch in channels):
        config.require_env(IG_ENV)
    for ch in channels:
        try:
            line, ok = do_approve(handle, ch)
            failed |= not ok
        except Exception as exc:
            failed = True
            text = http.redact(exc)
            line = f"- `{ch}`: **FAILED** — {text}"
            try:
                approval.send_failure_email(f"Publish failed ({ch}): {handle}", text)
            except Exception as mail_exc:
                line += f"\n  - (failure email also failed: {http.redact(mail_exc)})"
        lines.append(line)

    write_result(f"**{'⚠️ Approve' if failed else '✅ Approve'} `{handle}`**\n\n" + "\n".join(lines))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
