"""Environment handling."""

import os
import sys


def require_env(names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def public_raw_url(repo_path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["GITHUB_REF_NAME"]
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{repo_path}"


# --- draft selection ---------------------------------------------------------
# The scheduled draft job only drafts articles PUBLISHED AFTER this instant
# (UTC, set to the PR merge time). Anything older is drafted only on request:
# Actions -> Social Draft -> Run workflow -> article_id.
DRAFT_CUTOFF = "2026-10-31T13:54:28+00:00"

# Articles that must not be drafted by the cron OR on request, until their
# claims are fixed. {numeric article id: reason}. Remove an entry to release it.
HOLD_ARTICLE_IDS = {
    "751689728333": "bamboo-tencel-down-pillow: claims about down pillows; catalog has no down products",
    "751536243021": "bedding-certification-meaning-explained: Down Pass section; catalog has no down products",
}


# --- Pinterest ---------------------------------------------------------------
# The backfill (weekly drafts of all existing articles) and the daily scheduled
# publisher stay OFF until Pinterest approves Standard access. New-article pins
# (one per post, approved from the draft email) work on Trial as well.
PINTEREST_STANDARD_ACCESS = False
PIN_DAILY_CAP = 3          # max pins published per day (backfill schedule + daily publisher)
PIN_WEEKLY_MAX = 21        # 3 per day x 7 days = one weekly backfill batch
PIN_REFRESH_WARN_DAYS = 30  # email when the refresh token has fewer days left than this
