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

from core import config, http

# The ONLY accepted shape of an approval title. fullmatch(), never match():
# `$` would accept a trailing newline.
ACTION_RE = re.compile(
    r"(?:(approve|reject):[0-9]+:(ig|fb|ig\+fb|pin|reddit)"
    r"|(approve-batch|reject-batch):[0-9]{8}:pin)"   # weekly Pinterest backfill batch, id = YYYYMMDD
)


def approval_link(action, article_id, channels):
    """article_id is the NUMERIC Shopify id (handles get renamed; ids do not). For the weekly
    Pinterest batch, action is approve-batch / reject-batch and article_id is the batch date YYYYMMDD."""
    title = f"{action}:{article_id}:{channels}"
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


def pending_section(pending):
    """pending: [(article_id, channels "ig" / "ig+fb", title, handle)] -> HTML list with Approve links."""
    if not pending:
        return ""
    e = html.escape
    items = "".join(
        f"<li>{e(title or handle or aid)} <span style='color:#666'>({e(handle or aid)}, "
        f"{e(channels.replace('+', ' + ').upper())})</span> "
        f"{_button('Approve both' if channels == 'ig+fb' else 'Approve ' + channels.upper(), approval_link('approve', aid, channels), '#2e6b3a')}</li>"
        for aid, channels, title, handle in pending
    )
    return (f"<hr><h3>Pending approvals ({len(pending)})</h3>"
            f"<p style='color:#666;font-size:12px'>Other drafts still waiting. They stay approvable; "
            f"newer drafts never replace them.</p><ul>{items}</ul>")


def send_draft_email(article, previews, buttons, pending=None):
    """previews: [{label, image_url, text, target, link}], buttons: [(label, action, channels)],
    pending: see pending_section()."""
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
        _button(label, approval_link(action, article["article_id"], channels),
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
    {pending_section(pending)}
    """
    _send(f"Review + Approve: {article['title']}", body)


def send_failure_email(context, error_text):
    e = html.escape
    _send(
        f"[FAILED] {context}",
        f"<h2>Social pipeline failure</h2><p><strong>{e(context)}</strong></p>"
        f"<pre style='white-space:pre-wrap'>{e(http.redact(error_text))}</pre>",
    )


# --- Pinterest emails ---------------------------------------------------------

def _pin_block(draft, image_url, width=280):
    e = html.escape
    return f"""
    <table style="border-collapse:collapse"><tr>
      <td style="vertical-align:top;padding-right:16px"><img src="{e(image_url, quote=True)}" style="width:{width}px" /></td>
      <td style="vertical-align:top;font-family:sans-serif">
        <p style="margin:0 0 6px"><strong>{e(draft['title'])}</strong> <span style="color:#666">({len(draft['title'])}/100)</span></p>
        <p style="margin:0 0 8px;white-space:pre-wrap">{e(draft['description'])} <span style="color:#666">({len(draft['description'])}/500)</span></p>
        <p style="margin:0;color:#666">Board: <strong>{e(draft['board'])}</strong><br>Link: {e(draft['article_url'])}</p>
      </td></tr></table>"""


def send_pin_email(article, draft, image_url):
    """Own Pinterest draft email for one article: separate Approve / Reject."""
    aid = article["article_id"]
    btns = (_button("Approve", approval_link("approve", aid, "pin"), "#2e6b3a")
            + _button("Reject", approval_link("reject", aid, "pin"), "#8a2f2f"))
    body = f"""
    <h2>Pinterest: {html.escape(article['title'])}</h2>
    {_pin_block(draft, image_url, 300)}
    <hr><p>{btns}</p>
    <p style="color:#666;font-size:12px">A button opens a prefilled GitHub issue. Click "Submit new issue" to confirm.
    Nothing is created on Pinterest until you approve. (Trial access: pins are visible only to the app owner.)</p>
    """
    _send(f"Review + Approve (Pinterest): {article['title']}", body)


def send_pin_batch_email(batch, items, image_url_of):
    """Weekly backfill email. items: [{article_id, draft, publish_on}]. One approval for the week
    (approve all) or per pin; approved pins are published at most PIN_DAILY_CAP per day on publish_on."""
    e = html.escape
    rows = "".join(
        f"""<tr><td style="padding:10px 0;border-top:1px solid #ddd">
          <div style="color:#666;font-family:sans-serif;margin-bottom:6px">Publish on {e(i['publish_on'])}</div>
          {_pin_block(i['draft'], image_url_of(i['draft']), 160)}
          <p style="margin:8px 0 0">{_button('Approve', approval_link('approve', i['article_id'], 'pin'), '#2e6b3a')}
             {_button('Reject', approval_link('reject', i['article_id'], 'pin'), '#8a2f2f')}</p>
        </td></tr>"""
        for i in items
    )
    all_btns = (_button(f"Approve all {len(items)}", approval_link("approve-batch", batch, "pin"), "#2e6b3a")
                + _button("Reject all", approval_link("reject-batch", batch, "pin"), "#8a2f2f"))
    body = f"""
    <h2>Pinterest backfill, week of {e(batch)}: {len(items)} pins</h2>
    <p style="font-family:sans-serif">Approving schedules a pin; the daily job publishes at most
    {config.PIN_DAILY_CAP} per day on the date shown. Held articles are never included.</p>
    <p>{all_btns}</p>
    <table style="border-collapse:collapse;width:100%">{rows}</table>
    <hr><p>{all_btns}</p>
    """
    _send(f"Pinterest backfill {batch}: approve {len(items)} pins", body)
