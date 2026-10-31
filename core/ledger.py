"""State: drafts, ledger, and the git plumbing that persists them.

state/ledger.json:  {handle: {channel: {status, post_id, url, timestamp, ...}}}
state/drafts/{handle}/{channel}.json:  copy + image + target + link

Statuses:
  draft       copy saved, nothing on the platform
  publishing  media_publish was called but not yet verified. Blocks automatic
              retries: re-running could double-post. Needs a manual check.
  published   posted AND read back by id
  rejected    rejected in the approval step
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("state/ledger.json")
DRAFTS_DIR = Path("state/drafts")
LEGACY_PATH = Path("state/legacy-ig-drafted-articles.json")

DRAFT, PUBLISHING, PUBLISHED, REJECTED = "draft", "publishing", "published", "rejected"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    return json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}


def save(ledger):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def get(ledger, handle, channel):
    return ledger.get(handle, {}).get(channel)


def set_entry(ledger, handle, channel, **fields):
    entry = ledger.setdefault(handle, {}).setdefault(channel, {})
    entry.update(fields)
    return entry


def legacy_drafted_ids():
    """Article ids drafted by the pre-ledger Instagram pipeline."""
    if LEGACY_PATH.exists():
        return set(json.loads(LEGACY_PATH.read_text()).get("drafted_article_ids", []))
    return set()


def draft_path(handle, channel):
    return DRAFTS_DIR / handle / f"{channel}.json"


def write_draft(handle, channel, draft):
    path = draft_path(handle, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n")
    return path


def read_draft(handle, channel):
    path = draft_path(handle, channel)
    if not path.exists():
        raise FileNotFoundError(f"No draft at {path}")
    return json.loads(path.read_text())


# --- git persistence -------------------------------------------------------

def _git(*args, check=True):
    return subprocess.run(["git", *args], check=check)


def git_identity():
    _git("config", "user.name", "social-pipeline-bot")
    _git("config", "user.email", "actions@users.noreply.github.com")


def commit_and_push(paths, message):
    """Commit + push specific paths (used for images, which must be public
    before anything can fetch them)."""
    git_identity()
    _git("add", *[str(p) for p in paths])
    if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
        return
    _git("commit", "-m", message)
    _git("pull", "--rebase", "origin", os.environ["GITHUB_REF_NAME"])
    _git("push")


def update(mutator, message, extra_paths=(), attempts=4):
    """Apply mutator(ledger) and persist the ledger (plus extra_paths).

    Concurrent runs (a draft cron and an approval, or two approvals) edit the
    same JSON file, so a plain rebase can conflict. On a rejected push we drop
    our local commit, re-sync to origin and re-apply the mutation — in CI
    only, where the checkout is disposable.
    """
    branch = os.environ["GITHUB_REF_NAME"]
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    git_identity()
    last = None
    for attempt in range(1, attempts + 1):
        _git("pull", "--rebase", "origin", branch)
        ledger = load()
        mutator(ledger)
        save(ledger)
        _git("add", str(LEDGER_PATH), *[str(p) for p in extra_paths])
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return ledger
        _git("commit", "-m", message)
        push = subprocess.run(["git", "push"])
        if push.returncode == 0:
            return ledger
        last = push.returncode
        print(f"  push rejected (attempt {attempt}/{attempts}), re-syncing")
        if not in_ci:
            break
        _git("fetch", "origin", branch)
        _git("reset", "--hard", f"origin/{branch}")
    raise RuntimeError(f"Could not push ledger update after {attempts} attempts (git exit {last})")
