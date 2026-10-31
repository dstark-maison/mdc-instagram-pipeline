"""
Maison de Cocon — social publish / reject stage.

Input: env TITLE, shaped exactly "<approve|reject>:<article_id>:<channels>"
(validated with a strict regex; <id> is the NUMERIC Shopify article id). It comes from an
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
    action, article_id, channels = title.split(":")
    return action, article_id, channels.split("+")


def do_reject(article_id, channel):
    data = ledger.load()
    entry = ledger.get(data, article_id, channel)
    if not entry:
        return f"- `{channel}`: no draft found, nothing to reject."
    if entry["status"] != ledger.DRAFT:
        return f"- `{channel}`: status is `{entry['status']}`, left unchanged."

    def mutate(d):
        ledger.set_entry(d, article_id, channel, status=ledger.REJECTED, timestamp=ledger.now())

    ledger.update(mutate, f"Reject {article_id} ({channel}) [skip ci]")
    return f"- `{channel}`: draft rejected."


class Blocked(RuntimeError):
    """The approval cannot proceed. Reported like any other failure (email + issue comment)."""


def label(article_id):
    """'id (handle)' for messages; the handle is display-only."""
    for entry in ledger.load().get(article_id, {}).values():
        if entry.get("handle"):
            return f"{article_id} ({entry['handle']})"
    return article_id


def do_approve(article_id, channel):
    """Returns (markdown line, ok). Raises on anything that blocks or breaks publishing."""
    module = CHANNELS.get(channel)
    if not module:
        raise Blocked(f"Channel `{channel}` is not implemented in this phase.")

    data = ledger.load()
    entry = ledger.get(data, article_id, channel)
    if not entry:
        raise Blocked(f"No draft in the ledger for article {article_id} / {channel}.")
    if entry["status"] == ledger.PUBLISHED:
        return f"- `{channel}`: already published, nothing done → {entry['url']}", True
    if entry["status"] == ledger.PUBLISHING:
        raise Blocked(
            "A previous run reached the publish call but never verified the post "
            "(container " + str(entry.get("container_id")) + "). Not retrying automatically: "
            "it could double-post.\n\n" + ledger.stuck_entry_help(article_id, channel, entry))
    if entry["status"] == ledger.REJECTED:
        raise Blocked("This draft was rejected; it will not be published.")

    draft = ledger.read_draft(article_id, channel)

    def mark_publishing(container_id):
        def mutate(d):
            ledger.set_entry(d, article_id, channel, status=ledger.PUBLISHING,
                             container_id=container_id, timestamp=ledger.now())
        ledger.update(mutate, f"Publishing {article_id} ({channel}) [skip ci]")

    def revert_publishing():
        def mutate(d):
            ledger.set_entry(d, article_id, channel, status=ledger.DRAFT, container_id=None,
                             timestamp=ledger.now())
        ledger.update(mutate, f"Publish refused for {article_id} ({channel}), back to draft [skip ci]")

    result = module.publish(draft, mark_publishing, revert_publishing)

    def mutate(d):
        ledger.set_entry(d, article_id, channel, status=ledger.PUBLISHED, post_id=result["post_id"],
                         url=result["url"], timestamp=result["timestamp"] or ledger.now())

    ledger.update(mutate, f"Published {article_id} ({channel}) [skip ci]")
    line = f"- `{channel}`: published and verified → {result['url']}"

    ok = True
    try:
        extra = module.after_publish(draft, result)
        if extra:
            line += f"\n  - {extra}"
    except Exception as exc:
        ok = False
        line += f"\n  - **post-publish step failed** (the post itself is live): {http.redact(exc)}"
        approval.send_failure_email(f"Post live, follow-up step failed ({channel}): {article_id}", str(exc))
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
        action, article_id, channels = parse_title(title)
    except ValueError as exc:
        write_result(f"❌ {exc}")
        sys.exit(1)

    name = label(article_id)
    lines, failed = [], False
    if action == "reject":
        for ch in channels:
            lines.append(do_reject(article_id, ch))
        write_result(f"**Reject `{name}`**\n\n" + "\n".join(lines))
        return

    if any(ch in CHANNELS for ch in channels):
        config.require_env(IG_ENV)
    for ch in channels:
        try:
            line, ok = do_approve(article_id, ch)
            failed |= not ok
        except Exception as exc:
            failed = True
            text = http.redact(exc)
            # A failure after media_publish leaves the entry in `publishing`:
            # attach the exact ledger fix (Blocked already carries it).
            entry = ledger.get(ledger.load(), article_id, ch)
            if entry and entry.get("status") == ledger.PUBLISHING and "LEDGER ENTRY TO FIX" not in text:
                text += "\n\n" + ledger.stuck_entry_help(article_id, ch, entry)
            line = f"- `{ch}`: **FAILED** — {text}"
            try:
                approval.send_failure_email(f"Publish failed ({ch}): {name}", text)
            except Exception as mail_exc:
                line += f"\n  - (failure email also failed: {http.redact(mail_exc)})"
        lines.append(line)

    write_result(f"**{'⚠️ Approve' if failed else '✅ Approve'} `{name}`**\n\n" + "\n".join(lines))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
