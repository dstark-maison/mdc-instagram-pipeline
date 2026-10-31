"""Approval email (Resend) and the issue-based Approve / Reject buttons.

Email buttons are plain links, so they cannot call the workflow_dispatch API.
Each button opens a prefilled GitHub issue (label "approval", title
"<action>:<handle>:<channels>"); submitting it triggers social-publish.yml,
which only trusts OWNER/COLLABORATOR authors.
"""

import html
import os
import re
from urllib.parse import quote, urlencode

from core import http

# The ONLY accepted shape of an approval title. fullmatch(), never match():
# `$` would accept a trailing newline.
ACTION_RE = re.compile(r"(approve|reject):[a-z0-9-]+:(ig|fb|ig\+fb|pin|reddit)")


def approval_link(action, handle, channels):
    title = f"{action}:{handle}:{channels}"
    if not ACTION_RE.fullmatch(title):
        raise ValueError(f"Approval title would be rejected by the workflow: {title!r}")
    query = urlencode(
        {
            "labels": "approval",
            "title": title,
            "body": "Submit this issue to confirm. Do not edit the title.",
        },
        quote_via=quote,
    )
    return f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/issues/new?{query}"


def _send(subject, body_html):
    http.request(
        "POST", "https://api.resend.com/emails", "resend",
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "from": os.environ["RESEND_FROM"],
            "to": [os.environ["RESEND_TO"]],
            "subject": subject,
            "html": body_html,
        },
    )


def _button(label, url, color):
    return (
        f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;'
        f'padding:10px 18px;margin:4px 6px 4px 0;background:{color};color:#fff;'
        f'text-decoration:none;font-family:sans-serif">{html.escape(label)}</a>'
    )


def send_draft_email(article, previews, buttons):
    """previews: [{label, image_url, text, target}], buttons: [(label, action, channels)]."""
    e = html.escape
    blocks = "".join(
        f"""
        <h3>{e(p['label'])}</h3>
        <p><img src="{e(p['image_url'], quote=True)}" style="max-width:400px" /></p>
        <p style="white-space:pre-wrap">{e(p['text'])}</p>
        <p style="color:#666">Target: {e(p['target'])}<br>Link: {e(p['link'])}</p>
        """
        for p in previews
    )
    btns = "".join(
        _button(label, approval_link(action, article["handle"], channels),
                "#2e6b3a" if action == "approve" else "#8a2f2f")
        for label, action, channels in buttons
    )
    body = f"""
    <h2>Review: {e(article['title'])}</h2>
    {blocks}
    <hr>
    <p>{btns}</p>
    <p style="color:#666;font-size:12px">A button opens a prefilled GitHub issue.
    Click "Submit new issue" to confirm (you must be signed in as a repo
    collaborator). Nothing publishes on its own.</p>
    """
    _send(f"Review + Approve: {article['title']}", body)


def send_failure_email(context, error_text):
    e = html.escape
    _send(
        f"[FAILED] {context}",
        f"<h2>Social pipeline failure</h2><p><strong>{e(context)}</strong></p>"
        f"<pre style='white-space:pre-wrap'>{e(http.redact(error_text))}</pre>",
    )
